from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Build, BuildMod, Compatibility, Dependency, Mod, ModRelease
from .cache import CacheManager
from .versioning import is_version_compatible, version_key


@dataclass
class Issue:
    severity: str
    kind: str
    mod_id: int
    mod_name: str
    message: str
    target_name: str | None = None


class BuildService:
    def __init__(self, cache: CacheManager, moddb_client, coordinator=None):
        self.cache = cache
        self.moddb = moddb_client
        self.coordinator = coordinator

    async def resolve_selection(self, requested_ids: list[int]) -> tuple[list[int], dict[int, bool], dict[int, list[str]]]:
        roots = list(dict.fromkeys(requested_ids))
        async with SessionLocal() as session:
            all_mods = (await session.execute(select(Mod))).scalars().all()
            deps = (await session.execute(select(Dependency))).scalars().all()
        mods = {m.id: m for m in all_mods}
        threshold = max(0.0, min(1.0, settings.relation_min_confidence))
        required_edges: dict[int, list[tuple[int, str]]] = {}
        for d in deps:
            if d.relation_type != "dependency" or d.confidence < threshold or not d.target_mod_id:
                continue
            source = mods.get(d.mod_id)
            target = mods.get(d.target_mod_id)
            if source and target:
                required_edges.setdefault(source.id, []).append((target.id, source.name))
        # Selecting a thread add-on selects its parent mod too.
        for mod in all_mods:
            if mod.parent_mod_id and mod.parent_mod_id in mods:
                required_edges.setdefault(mod.id, []).append((mod.parent_mod_id, mod.name))

        state: dict[int, int] = {}
        order: list[int] = []
        auto_added: dict[int, bool] = {}
        reasons: dict[int, list[str]] = {}

        def visit(mod_id: int, explicit: bool = False):
            if mod_id not in mods:
                return
            if explicit:
                auto_added.setdefault(mod_id, False)
                if auto_added.get(mod_id) is False:
                    pass
            mark = state.get(mod_id, 0)
            if mark == 1:
                return
            if mark == 2:
                if explicit:
                    auto_added[mod_id] = False
                return
            state[mod_id] = 1
            for target_id, source_name in required_edges.get(mod_id, []):
                reasons.setdefault(target_id, [])
                if source_name not in reasons[target_id]:
                    reasons[target_id].append(source_name)
                if target_id not in auto_added:
                    auto_added[target_id] = True
                visit(target_id, False)
            state[mod_id] = 2
            if mod_id not in order:
                order.append(mod_id)
            if explicit:
                auto_added[mod_id] = False

        for root in roots:
            visit(root, True)
        return order, auto_added, reasons

    async def inspect(self, mod_ids: list[int], target_game_version: str | None) -> dict:
        requested_ids = list(dict.fromkeys(mod_ids))
        resolved_ids, auto_added, reasons = await self.resolve_selection(requested_ids)
        async with SessionLocal() as session:
            mods = (
                await session.execute(
                    select(Mod).options(selectinload(Mod.releases)).where(Mod.id.in_(resolved_ids))
                )
            ).scalars().all()
            selected = {m.id: m for m in mods}
            issues: list[Issue] = []
            dependencies = (
                await session.execute(select(Dependency).where(Dependency.mod_id.in_(resolved_ids)))
            ).scalars().all()
            compat = (
                await session.execute(select(Compatibility).where(Compatibility.mod_id.in_(resolved_ids)))
            ).scalars().all()
            all_mods_by_id = {m.id: m for m in (await session.execute(select(Mod))).scalars().all()}
        missing_requested = [mid for mid in requested_ids if mid not in selected]
        for mid in missing_requested:
            issues.append(Issue("error", "missing_mod", mid, f"Мод #{mid}", "Мод отсутствует в локальном каталоге."))

        threshold = max(0.0, min(1.0, settings.relation_min_confidence))
        dependencies = [d for d in dependencies if d.confidence >= threshold]
        compat = [c for c in compat if c.confidence >= threshold]

        for d in dependencies:
            if d.relation_type != "dependency":
                continue
            source = selected.get(d.mod_id)
            if not source:
                continue
            target = all_mods_by_id.get(d.target_mod_id) if d.target_mod_id else None
            if not target:
                if d.target_name:
                    issues.append(Issue("error", "unknown_dependency", d.mod_id, source.name, f"Не удалось сопоставить обязательную зависимость: {d.target_name}", d.target_name))
                continue
            if target.id not in selected:
                issues.append(Issue("error", "missing_dependency", d.mod_id, source.name, f"Не выбрана обязательная зависимость: {target.name}", target.name))
                continue
            if d.required_version:
                release = _release_for_game(target.releases, target_game_version)
                if release is None:
                    issues.append(Issue("error", "dependency_version", source.id, source.name, f"Для зависимости {target.name} нет подходящего релиза.", target.name))
                elif version_key(release.mod_version) < version_key(d.required_version):
                    issues.append(Issue("error", "dependency_version", source.id, source.name, f"Зависимость {target.name} требует версию {d.required_version} или новее, доступно {release.mod_version}.", target.name))

        for c in compat:
            if c.target_mod_id in selected and c.relation_type == "incompatible":
                source = selected.get(c.mod_id)
                target = selected.get(c.target_mod_id)
                if source and target:
                    issues.append(Issue("error", "incompatible", c.mod_id, source.name, f"Конфликт с {target.name}", target.name))

        for m in mods:
            if target_game_version and _release_for_game(m.releases, target_game_version) is None:
                issues.append(Issue("error", "game_version", m.id, m.name, f"Нет релиза для ветки Vintage Story {target_game_version} (проверка по major.minor)."))

        return {
            "issues": [i.__dict__ for i in issues],
            "ok": not any(i.severity == "error" for i in issues),
            "relation_confidence_threshold": threshold,
            "resolved_mod_ids": resolved_ids,
            "auto_added_mod_ids": [mid for mid in resolved_ids if auto_added.get(mid)],
            "auto_reasons": reasons,
        }

    async def create(self, mod_ids: list[int], target_game_version: str | None, name: str | None = None) -> Build:
        async def inner() -> Build:
            resolved_ids, auto_added, _reasons = await self.resolve_selection(mod_ids)
            async with SessionLocal() as session:
                mods = (
                    await session.execute(
                        select(Mod).options(selectinload(Mod.releases)).where(Mod.id.in_(resolved_ids))
                    )
                ).scalars().all()
            by_id = {m.id: m for m in mods}

            api_data: dict = {}
            if target_game_version:
                specs = [by_id[mid].mod_id for mid in resolved_ids if mid in by_id and by_id[mid].mod_id]
                if specs:
                    try:
                        result = await self.moddb.get_install_information(specs, target_game_version)
                        api_data = result.get("data", result) if isinstance(result, dict) else {}
                    except Exception:
                        api_data = {}

            async with SessionLocal() as session:
                build = Build(
                    share_id=secrets.token_urlsafe(8),
                    name=(name or "Vintage Story Modpack").strip()[:160],
                    target_game_version=target_game_version,
                    published=False,
                )
                session.add(build)
                await session.flush()
                for pos, mod_id in enumerate(resolved_ids):
                    mod = by_id[mod_id]
                    release = _release_for_game(mod.releases, target_game_version)
                    selected_version = release.mod_version if release else None
                    info = api_data.get(mod.mod_id or "", {}) if isinstance(api_data, dict) else {}
                    if isinstance(info, dict) and info.get("fileName"):
                        for candidate in mod.releases:
                            if candidate.filename == info["fileName"]:
                                release = candidate
                                selected_version = candidate.mod_version
                                break
                    session.add(
                        BuildMod(
                            build_id=build.id,
                            mod_id=mod.id,
                            release_id=release.id if release else None,
                            selected_version=selected_version,
                            auto_added=bool(auto_added.get(mod_id)),
                            position=pos,
                        )
                    )
                await session.commit()
                await session.refresh(build)
                return build

        if self.coordinator:
            async with self.coordinator.user_priority():
                return await inner()
        return await inner()

    async def prepare(self, share_id: str, progress=None) -> None:
        async with SessionLocal() as session:
            build_version = (await session.execute(select(Build.target_game_version).where(Build.share_id == share_id))).scalar_one_or_none()
        if self.coordinator:
            async with self.coordinator.user_priority():
                await self._prepare_inner(share_id, progress, build_version)
        else:
            await self._prepare_inner(share_id, progress, build_version)

    async def _prepare_inner(self, share_id: str, progress, build_version: str | None):
        async with SessionLocal() as session:
            build = (await session.execute(select(Build).where(Build.share_id == share_id))).scalar_one()
            items = (
                await session.execute(
                    select(BuildMod, Mod, ModRelease)
                    .join(Mod, Mod.id == BuildMod.mod_id)
                    .outerjoin(ModRelease, ModRelease.id == BuildMod.release_id)
                    .where(BuildMod.build_id == build.id)
                    .order_by(BuildMod.position)
                )
            ).all()
            paths = []
            for _bm, mod, release in items:
                url = release.file_url if release else mod.latest_file_url
                file_name = release.filename if release else mod.latest_file_name
                if not url:
                    raise RuntimeError(f"Нет URL файла для {mod.name}")
                paths.append((mod.name, url, file_name or f"{mod.mod_id or mod.id}.zip", mod.id, release.id if release else None))

        sem = asyncio.Semaphore(max(1, self.cache.download_concurrency))
        completed = 0
        lock = asyncio.Lock()

        async def fetch(item):
            nonlocal completed
            name, url, file_name, owner_mod_id, release_id = item
            async with sem:
                await self.cache.fetch_file(url, file_name, owner_mod_id=owner_mod_id, owner_release_id=release_id, game_version=build_version)
            async with lock:
                completed += 1
                count = completed
            if progress:
                await progress(count, len(paths), f"Файл {count}/{len(paths)}: {name}")

        await asyncio.gather(*(fetch(item) for item in paths))
        if progress:
            await progress(len(paths), len(paths), "Все файлы загружены в кеш. ZIP создастся при скачивании.")

    async def _fetch_build_file(self, item, sem, build_version):
        name, url, filename, owner_mod_id, release_id = item
        async with sem:
            path = await self.cache.fetch_file(
                url,
                filename,
                owner_mod_id=owner_mod_id,
                owner_release_id=release_id,
                game_version=build_version,
            )
        return path, filename, name

    async def build_temp_archive(self, share_id: str) -> tuple[Path, str]:
        async def inner():
            async with SessionLocal() as session:
                build = (await session.execute(select(Build).where(Build.share_id == share_id))).scalar_one()
                items = (
                    await session.execute(
                        select(BuildMod, Mod, ModRelease)
                        .join(Mod, Mod.id == BuildMod.mod_id)
                        .outerjoin(ModRelease, ModRelease.id == BuildMod.release_id)
                        .where(BuildMod.build_id == build.id)
                        .order_by(BuildMod.position)
                    )
                ).all()
                missing = []
                resolved = []
                for _bm, mod, release in items:
                    url = release.file_url if release else mod.latest_file_url
                    filename = release.filename if release else mod.latest_file_name
                    if not url:
                        raise RuntimeError(f"Нет URL файла для {mod.name}")
                    safe_filename = filename or f"{mod.mod_id or mod.id}.zip"
                    cached = await self.cache.cached_path(url, safe_filename)
                    if cached is None:
                        missing.append((mod.name, url, safe_filename, mod.id, release.id if release else None))
                    else:
                        resolved.append((cached, safe_filename, mod.name))
                target_version = build.target_game_version
                build_name = build.name

            # Normally prefetch has already filled this cache. If a user requests a build before
            # prefetch reaches all files, finish the missing files now under user priority rather
            # than failing with a confusing "prepare first" error.
            if missing:
                sem = asyncio.Semaphore(max(1, self.cache.download_concurrency))
                downloaded = await asyncio.gather(
                    *(
                        self._fetch_build_file(item, sem, target_version)
                        for item in missing
                    )
                )
                resolved.extend(downloaded)

            temp = tempfile.NamedTemporaryFile(prefix="vs-modpack-", suffix=".zip", delete=False)
            temp_path = Path(temp.name)
            temp.close()
            manifest = {"share_id": share_id, "name": build_name, "target_game_version": target_version, "mods": [{"filename": filename, "name": name} for _, filename, name in resolved]}
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for path, filename, _name in resolved:
                    zf.write(path, arcname=filename)
                zf.writestr("modpack.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            return temp_path, f"{_safe_filename(build_name)}.zip"
        if self.coordinator:
            async with self.coordinator.user_priority():
                return await inner()
        return await inner()


def _safe_filename(value: str) -> str:
    import re
    value = re.sub(r"[^A-Za-z0-9А-Яа-яЁё _.-]+", "", value).strip()
    return value or "vintage-story-modpack"


def _release_for_game(releases, target: str | None):
    if not releases:
        return None
    candidates = []
    for release in releases:
        try:
            tags = json.loads(release.game_versions_json or "[]")
        except json.JSONDecodeError:
            tags = []
        if not target or is_version_compatible([str(tag) for tag in tags], target):
            candidates.append(release)
    return max(candidates, key=lambda r: (version_key(r.mod_version), str(r.created_at or "")), default=None)
