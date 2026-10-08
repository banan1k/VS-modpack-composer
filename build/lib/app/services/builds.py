from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
import zipfile
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
    def __init__(self, cache: CacheManager, moddb_client):
        self.cache = cache
        self.moddb = moddb_client

    async def inspect(self, mod_ids: list[int], target_game_version: str | None) -> dict:
        requested_ids = list(dict.fromkeys(mod_ids))
        async with SessionLocal() as session:
            mods = (
                await session.execute(
                    select(Mod).options(selectinload(Mod.releases)).where(Mod.id.in_(requested_ids))
                )
            ).scalars().all()
            selected = {m.id: m for m in mods}
            issues: list[Issue] = []
            dependencies = (
                await session.execute(select(Dependency).where(Dependency.mod_id.in_(requested_ids)))
            ).scalars().all()
            compat = (
                await session.execute(select(Compatibility).where(Compatibility.mod_id.in_(requested_ids)))
            ).scalars().all()

            all_target_ids = {
                row.target_mod_id
                for row in [*dependencies, *compat]
                if row.target_mod_id
            }
            targets = {}
            if all_target_ids:
                targets = {
                    m.id: m
                    for m in (
                        await session.execute(select(Mod).where(Mod.id.in_(all_target_ids)))
                    ).scalars().all()
                }

        missing_requested = [mid for mid in requested_ids if mid not in selected]
        for mid in missing_requested:
            issues.append(
                Issue(
                    "error",
                    "missing_mod",
                    mid,
                    f"Мод #{mid}",
                    "Мод отсутствует в локальном каталоге.",
                )
            )

        confidence_threshold = max(0.0, min(1.0, settings.relation_min_confidence))

        # Only high-confidence relations participate in build validation. Lower-confidence
        # detections remain visible in the UI for manual inspection.
        dependencies = [d for d in dependencies if d.confidence >= confidence_threshold]
        compat = [c for c in compat if c.confidence >= confidence_threshold]

        for d in dependencies:
            if d.relation_type not in {"dependency", "optional_dependency"}:
                continue
            source = selected.get(d.mod_id)
            if not source:
                continue
            target = targets.get(d.target_mod_id) if d.target_mod_id else None

            if d.relation_type == "dependency":
                if d.target_mod_id and d.target_mod_id not in selected:
                    issues.append(
                        Issue(
                            "warning",
                            "missing_dependency",
                            d.mod_id,
                            source.name,
                            f"Не выбрана обязательная зависимость: {target.name if target else (d.target_name or 'неизвестный мод')}",
                            target.name if target else d.target_name,
                        )
                    )
                elif not d.target_mod_id and d.target_name:
                    issues.append(
                        Issue(
                            "warning",
                            "unknown_dependency",
                            d.mod_id,
                            source.name,
                            f"Не удалось сопоставить обязательную зависимость: {d.target_name}",
                            d.target_name,
                        )
                    )
            elif d.target_mod_id and d.target_mod_id not in selected:
                issues.append(
                    Issue(
                        "warning",
                        "optional_dependency",
                        d.mod_id,
                        source.name,
                        f"Опциональная зависимость не выбрана: {target.name if target else (d.target_name or 'неизвестный мод')}",
                        target.name if target else d.target_name,
                    )
                )

        for c in compat:
            if c.target_mod_id in selected and c.relation_type == "incompatible":
                source = selected.get(c.mod_id)
                target = selected.get(c.target_mod_id)
                if source and target:
                    issues.append(
                        Issue(
                            "error",
                            "incompatible",
                            c.mod_id,
                            source.name,
                            f"Конфликт с {target.name}",
                            target.name,
                        )
                    )

        for m in mods:
            if not target_game_version:
                continue
            if _release_for_game(m.releases, target_game_version) is None:
                issues.append(
                    Issue(
                        "error",
                        "game_version",
                        m.id,
                        m.name,
                        f"Нет релиза для ветки Vintage Story {target_game_version} (проверка по major.minor).",
                    )
                )

        return {
            "issues": [i.__dict__ for i in issues],
            "ok": not any(i.severity == "error" for i in issues),
            "relation_confidence_threshold": confidence_threshold,
        }

    async def create(self, mod_ids: list[int], target_game_version: str | None, name: str | None = None) -> Build:
        async with SessionLocal() as session:
            mods = (
                await session.execute(
                    select(Mod).options(selectinload(Mod.releases)).where(Mod.id.in_(mod_ids))
                )
            ).scalars().all()
        by_id = {m.id: m for m in mods}

        # V2 is used as a file resolver, not as the build compatibility gate. This is
        # intentional because our build validation works at major.minor precision.
        api_data: dict = {}
        if target_game_version:
            specs = [m.mod_id for m in mods if m.mod_id]
            if specs:
                try:
                    result = await self.moddb.get_install_information(specs, target_game_version)
                    api_data = result.get("data", result)
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
            for pos, mod_id in enumerate(mod_ids):
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
                        position=pos,
                    )
                )
            await session.commit()
            await session.refresh(build)
            return build

    async def prepare(self, share_id: str, progress=None) -> None:
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
                paths.append((mod.name, url, file_name or f"{mod.mod_id or mod.id}.zip"))

        sem = asyncio.Semaphore(max(1, self.cache.download_concurrency))
        completed = 0
        lock = asyncio.Lock()

        async def fetch(item):
            nonlocal completed
            name, url, file_name = item
            async with sem:
                await self.cache.fetch_file(url, file_name)
            async with lock:
                completed += 1
                count = completed
            if progress:
                await progress(count, len(paths), f"Файл {count}/{len(paths)}: {name}")

        await asyncio.gather(*(fetch(item) for item in paths))
        if progress:
            await progress(len(paths), len(paths), "Все файлы загружены в кеш. ZIP создастся при скачивании.")

    async def build_temp_archive(self, share_id: str) -> tuple[Path, str]:
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
            resolved = []
            for _bm, mod, release in items:
                url = release.file_url if release else mod.latest_file_url
                filename = release.filename if release else mod.latest_file_name
                if not url:
                    raise RuntimeError(f"Нет URL файла для {mod.name}")
                cached = await self.cache.cached_path(url, filename or f"{mod.mod_id or mod.id}.zip")
                if cached is None:
                    raise RuntimeError(f"Файл {mod.name} не найден в кеше. Сначала подготовьте ZIP.")
                resolved.append((cached, filename or cached.name, mod.name))
            target_version = build.target_game_version
            build_name = build.name

        temp = tempfile.NamedTemporaryFile(prefix="vs-modpack-", suffix=".zip", delete=False)
        temp_path = Path(temp.name)
        temp.close()
        manifest = {
            "share_id": share_id,
            "name": build_name,
            "target_game_version": target_version,
            "mods": [{"filename": filename, "name": name} for _, filename, name in resolved],
        }
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path, filename, _name in resolved:
                zf.write(path, arcname=filename)
            zf.writestr("modpack.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return temp_path, f"{_safe_filename(build_name)}.zip"


def _safe_filename(value: str) -> str:
    import re

    value = re.sub(r"[^A-Za-z0-9А-Яа-яЁё _.-]+", "", value).strip()
    return value or "vintage-story-modpack"


def _release_for_game(releases, target: str | None):
    if not releases:
        return None
    candidates = []
    for release in releases:
        tags = json.loads(release.game_versions_json or "[]")
        if not target or is_version_compatible([str(tag) for tag in tags], target):
            candidates.append(release)
    return max(
        candidates,
        key=lambda r: (version_key(r.mod_version), str(r.created_at or "")),
        default=None,
    )
