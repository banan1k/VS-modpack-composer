from __future__ import annotations

import asyncio
import json
import logging
import secrets
import tempfile
import zipfile
from urllib.parse import urljoin
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Build, BuildMod, CachedFile, Compatibility, Dependency, Mod, ModRelease
from .cache import CacheManager
from .versioning import is_version_compatible, version_key

logger = logging.getLogger(__name__)


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
            all_mods_by_id = {
                m.id: m for m in (await session.execute(
                    select(Mod).options(selectinload(Mod.releases))
                )).scalars().all()
            }
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
                version_parts = target_game_version.split(".")
                branch = ".".join(version_parts[:2]) if len(version_parts) >= 2 else target_game_version
                latest_mod_version = max(
                    (release.mod_version for release in m.releases if release.mod_version),
                    key=version_key,
                    default=None,
                )
                suffix = f" (последняя версия мода: {latest_mod_version})" if latest_mod_version else ""
                issues.append(Issue(
                    "error", "game_version", m.id, m.name,
                    f"Нет релиза для ветки Vintage Story {branch}{suffix}.",
                ))

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
                        api_data = _install_info_map(result)
                    except Exception:
                        logger.exception("[BUILD] V2 install-information failed while creating build")
                        api_data = {}

            # If the target-version endpoint has no file, it can mean the release is not tagged
            # for this game version (Fixed Paths is a real example). Compatibility inspection has
            # already warned the user before BuildService.create is called; if they chose to
            # continue, resolve the latest non-retracted release so the build can actually be
            # downloaded instead of failing later with an empty URL.
            latest_fallback: dict[int, dict] = {}
            unresolved = []
            for mid in resolved_ids:
                mod = by_id.get(mid)
                if mod is None or not mod.mod_db_id or not mod.mod_id:
                    continue
                info = api_data.get(str(mod.mod_id).casefold(), {})
                api_url = _absolute_file_url(_info_value(info, "fileUrl", "fileurl", "file_url", "url"), self.moddb)
                release = _release_for_game(mod.releases, target_game_version)
                if not api_url and not (release and release.file_url):
                    unresolved.append(mod)

            if unresolved and hasattr(self.moddb, "get_latest_release"):
                latest_sem = asyncio.Semaphore(4)

                async def latest_for_build(mod):
                    async with latest_sem:
                        try:
                            result = await self.moddb.get_latest_release(mod.mod_db_id, identifier=mod.mod_id)
                            url = _absolute_file_url(_info_value(result, "fileUrl", "fileurl", "file_url", "url"), self.moddb)
                            if url:
                                logger.warning(
                                    "[BUILD] using latest available release for %s despite no target-version install file (gv=%s, version=%s)",
                                    mod.name, target_game_version,
                                    _info_value(result, "version", "modVersion", "modversion") or "unknown",
                                )
                                return mod.id, result
                            logger.warning(
                                "[BUILD] latest release has no file for %s: %s",
                                mod.name, result.get("errorCode", "no fileUrl"),
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[BUILD] latest-release fallback failed for %s: %s", mod.name, exc)
                    return mod.id, {}

                latest_fallback = dict(await asyncio.gather(*(latest_for_build(mod) for mod in unresolved)))

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
                    info = api_data.get(str(mod.mod_id or "").casefold(), {})
                    latest_info = latest_fallback.get(mod.id, {})
                    api_url = _absolute_file_url(_info_value(info, "fileUrl", "fileurl", "file_url", "url"), self.moddb)
                    api_filename = _info_value(info, "fileName", "filename", "file_name")
                    api_version = _info_value(info, "modVersion", "modversion", "version")
                    if not api_url and latest_info:
                        api_url = _absolute_file_url(_info_value(latest_info, "fileUrl", "fileurl", "file_url", "url"), self.moddb)
                        api_filename = api_filename or _info_value(latest_info, "fileName", "filename", "file_name")
                        api_version = api_version or _info_value(latest_info, "version", "modVersion", "modversion")
                    if isinstance(api_filename, str):
                        api_filename = api_filename.strip() or None
                    else:
                        api_filename = None
                    matched_release = next((
                        candidate for candidate in mod.releases
                        if api_url and candidate.file_url == api_url
                    ), None)
                    if matched_release is None and api_filename:
                        same_name = [candidate for candidate in mod.releases if candidate.filename == api_filename]
                        matched_release = next((
                            candidate for candidate in same_name
                            if api_version and str(candidate.mod_version).strip() == str(api_version).strip()
                        ), None)
                        if matched_release is None and len(same_name) == 1:
                            matched_release = same_name[0]
                    if matched_release is not None:
                        release = matched_release
                    elif api_url:
                        # Exact V2 URL is authoritative; don't bind it to an unrelated V1 release
                        # merely because that release happened to be the latest local candidate.
                        release = None
                    # If no release matches the selected file and the user's confirmed build has
                    # a version warning, retain the latest downloadable V1 URL rather than storing
                    # an empty URL. The warning remains in inspect(); this is only the forced-build
                    # fallback used after the user chooses to continue.
                    fallback_release = _latest_downloadable_release(mod.releases)
                    selected_url = api_url or (release.file_url if release else None) or mod.latest_file_url or (fallback_release.file_url if fallback_release else None)
                    selected_filename = api_filename or (release.filename if release else None) or mod.latest_file_name or (fallback_release.filename if fallback_release else None)
                    selected_version = (
                        release.mod_version if release and not api_version
                        else str(api_version).strip() if api_version
                        else (release.mod_version if release else fallback_release.mod_version if fallback_release else None)
                    )
                    if not selected_url:
                        logger.error(
                            "[BUILD] no downloadable URL for %s (modid=%s, mod_db_id=%s, gv=%s, install_error=%s)",
                            mod.name, mod.mod_id, mod.mod_db_id, target_game_version,
                            _info_value(info, "errorCode", "errorcode") or "none",
                        )
                    session.add(
                        BuildMod(
                            build_id=build.id,
                            mod_id=mod.id,
                            release_id=release.id if release else None,
                            selected_version=selected_version,
                            selected_file_url=selected_url,
                            selected_file_name=selected_filename,
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
        items = await self._resolve_build_file_items(share_id)
        paths = []
        for item in items:
            if not item["url"]:
                raise RuntimeError(f"Нет URL файла для {item['name']}. Проверь релизы Mod DB или выполни обновление каталога.")
            paths.append((
                item["name"], item["url"], item["filename"], item["mod_id"], item["release_id"]
            ))

        sem = asyncio.Semaphore(max(1, self.cache.download_concurrency))
        completed = 0
        lock = asyncio.Lock()

        async def fetch(item):
            nonlocal completed
            name, url, file_name, owner_mod_id, release_id = item
            async with sem:
                await self.cache.fetch_file(
                    url, file_name, owner_mod_id=owner_mod_id,
                    owner_release_id=release_id, game_version=build_version,
                )
            async with lock:
                completed += 1
                count = completed
            if progress:
                await progress(count, len(paths), f"Файл {count}/{len(paths)}: {name}")

        await asyncio.gather(*(fetch(item) for item in paths))
        if progress:
            await progress(len(paths), len(paths), "Все файлы загружены в кеш. ZIP создастся при скачивании.")

    async def _resolve_build_file_items(self, share_id: str) -> list[dict]:
        """Resolve durable file URLs for a build, repairing older rows that only stored release IDs."""
        async with SessionLocal() as session:
            build = (await session.execute(select(Build).where(Build.share_id == share_id))).scalar_one()
            build_version = build.target_game_version
            buildmods = (await session.execute(
                select(BuildMod).where(BuildMod.build_id == build.id).order_by(BuildMod.position)
            )).scalars().all()
            mod_ids = list(dict.fromkeys(bm.mod_id for bm in buildmods))
            mods = (await session.execute(
                select(Mod).options(selectinload(Mod.releases)).where(Mod.id.in_(mod_ids))
            )).scalars().all()
            mods_by_id = {mod.id: mod for mod in mods}
            releases_by_id = {release.id: release for mod in mods for release in mod.releases}
            cache_rows = (await session.execute(
                select(CachedFile).where(CachedFile.owner_mod_id.in_(mod_ids)).order_by(CachedFile.id.desc())
            )).scalars().all() if mod_ids else []
            cache_by_mod: dict[int, list[CachedFile]] = {}
            for row in cache_rows:
                if row.owner_mod_id is not None:
                    cache_by_mod.setdefault(row.owner_mod_id, []).append(row)
            items = []
            for bm in buildmods:
                mod = mods_by_id.get(bm.mod_id)
                if mod is None:
                    continue
                release = releases_by_id.get(bm.release_id)
                # A legacy BuildMod can carry a release FK unrelated to its persisted V2 URL.
                # Do not assign that foreign release's version/id to another file.
                if release and bm.selected_file_url and release.file_url and release.file_url != bm.selected_file_url:
                    exact_match = next((r for r in mod.releases if r.file_url == bm.selected_file_url), None)
                    release = exact_match
                # Preserve the selected historical release if it exists. If its V1 row has no
                # file URL, let V2/local cache fallback resolve a downloadable file.
                url = bm.selected_file_url or (release.file_url if release else None)
                filename = bm.selected_file_name or (release.filename if release else None) or mod.latest_file_name or f"{mod.mod_id or mod.id}.zip"
                items.append({
                    "build_mod_id": bm.id,
                    "mod_id": mod.id,
                    "mod_identifier": mod.mod_id,
                    "mod_db_id": mod.mod_db_id,
                    "name": mod.name,
                    "url": url,
                    "cached_files": [
                        {"url": row.url, "filename": row.filename, "path": row.path,
                         "release_id": row.owner_release_id, "game_version": row.game_version}
                        for row in cache_by_mod.get(mod.id, [])
                    ],
                    "filename": filename,
                    "release_id": release.id if release else mod.latest_release_id,
                    "selected_version": bm.selected_version or (release.mod_version if release else None),
                    "latest_file_url": mod.latest_file_url,
                    "latest_file_name": mod.latest_file_name,
                    "releases": [
                        {
                            "id": rel.id,
                            "version": rel.mod_version,
                            "filename": rel.filename,
                            "url": rel.file_url,
                            "game_versions_json": rel.game_versions_json,
                            "created_at": str(rel.created_at or ""),
                        }
                        for rel in mod.releases
                    ],
                })

        missing = [item for item in items if not item["url"] and item["mod_identifier"]]
        if missing and build_version:
            try:
                result = await self.moddb.get_install_information(
                    [item["mod_identifier"] for item in missing], build_version
                )
                info_map = _install_info_map(result)
            except Exception as exc:  # noqa: BLE001
                info_map = {}
                import logging
                logging.getLogger(__name__).warning(
                    "[BUILD] V2 install-information fallback failed for %s: %s", share_id, exc
                )
            for item in missing:
                info = info_map.get(str(item["mod_identifier"]).casefold(), {})
                api_url = _absolute_file_url(_info_value(info, "fileUrl", "fileurl", "file_url", "url"), self.moddb)
                api_filename = _info_value(info, "fileName", "filename", "file_name")
                api_version = _info_value(info, "modVersion", "modversion", "version")
                matched = next((r for r in item["releases"] if api_url and r["url"] == api_url), None)
                if matched is None and api_filename:
                    same_name = [r for r in item["releases"] if r["filename"] == api_filename]
                    matched = next((
                        r for r in same_name
                        if api_version and str(r["version"]).strip() == str(api_version).strip()
                    ), None)
                    if matched is None and len(same_name) == 1:
                        matched = same_name[0]
                if api_url:
                    item["url"] = api_url
                    item["filename"] = api_filename or item["filename"]
                    if matched:
                        item["release_id"] = matched["id"]
                        item["selected_version"] = matched["version"]
                    else:
                        item["release_id"] = None
                        if api_version:
                            item["selected_version"] = str(api_version)

        # If install-information?gv=... had no file because the mod is not tagged for that
        # game version, ask V2 for the newest non-retracted release by its internal Mod DB ID.
        # This is the download fallback for a build whose compatibility warning the user chose
        # to override (for example, Fixed Paths whose page's tags are stale).
        latest_missing = [item for item in items if not item["url"] and item.get("mod_db_id") and item.get("mod_identifier")]
        if latest_missing and hasattr(self.moddb, "get_latest_release"):
            latest_sem = asyncio.Semaphore(4)

            async def latest_for_item(item):
                async with latest_sem:
                    try:
                        result = await self.moddb.get_latest_release(item["mod_db_id"], identifier=item["mod_identifier"])
                        url = _absolute_file_url(_info_value(result, "fileUrl", "fileurl", "file_url", "url"), self.moddb)
                        if url:
                            return item["build_mod_id"], result
                        logger.warning(
                            "[BUILD] latest release has no file for %s: %s",
                            item["name"], _info_value(result, "errorCode", "errorcode") or "no fileUrl",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[BUILD] latest-release fallback failed for %s: %s", item["name"], exc)
                return item["build_mod_id"], {}

            latest_map = dict(await asyncio.gather(*(latest_for_item(item) for item in latest_missing)))
            for item in latest_missing:
                result = latest_map.get(item["build_mod_id"], {})
                url = _absolute_file_url(_info_value(result, "fileUrl", "fileurl", "file_url", "url"), self.moddb)
                if not url:
                    continue
                filename = _info_value(result, "fileName", "filename", "file_name")
                version = _info_value(result, "version", "modVersion", "modversion")
                item["url"] = url
                item["filename"] = str(filename).strip() if filename else item["filename"]
                item["selected_version"] = str(version).strip() if version else item["selected_version"]
                match = next((r for r in item["releases"] if r["url"] == url), None)
                if match is None and filename:
                    same_name = [r for r in item["releases"] if r["filename"] == filename]
                    match = next((
                        r for r in same_name
                        if version and str(r["version"]).strip() == str(version).strip()
                    ), None)
                    if match is None and len(same_name) == 1:
                        match = same_name[0]
                item["release_id"] = match["id"] if match else None
                logger.warning(
                    "[BUILD] using latest release for %s after no file matched gv=%s (version=%s)",
                    item["name"], build_version, item["selected_version"] or "unknown",
                )

        # Cache metadata can outlive a missing/legacy ModRelease URL. Reuse an already cached
        # file by the owning Mod row before trying any network fallback. Prefer the same build
        # version, then the explicitly selected release, then the most recently cached file.
        for item in items:
            if item["url"]:
                continue
            candidates = item.get("cached_files", [])
            candidates = sorted(
                candidates,
                key=lambda r: (
                    1 if r.get("game_version") == build_version else 0,
                    1 if item.get("release_id") is not None and r.get("release_id") == item.get("release_id") else 0,
                ),
                reverse=True,
            )
            for cached in candidates:
                if not cached.get("url"):
                    continue
                item["url"] = cached["url"]
                item["filename"] = cached.get("filename") or item["filename"]
                if cached.get("release_id") is not None:
                    item["release_id"] = cached["release_id"]
                    release = next((r for r in item["releases"] if r["id"] == cached["release_id"]), None)
                    if release and release.get("version"):
                        item["selected_version"] = release["version"]
                logger.info("[BUILD] recovered URL for %s from cached file metadata", item["name"])
                break

        # Last fallback for historical builds: prefer the newest compatible V1 release, but if
        # none is tagged for this game version, retain the newest downloadable release. Build
        # compatibility checks have already issued a warning; this path allows an explicit
        # user override to download instead of stopping on an empty URL.
        for item in items:
            if item["url"]:
                continue
            releases = [rel for rel in item["releases"] if rel["url"]]
            compatible_releases = []
            for rel in releases:
                proxy = type("ReleaseProxy", (), rel)()
                if not build_version or _release_proxy_matches_game(proxy, build_version):
                    compatible_releases.append(rel)
            candidates = compatible_releases or releases
            if candidates:
                chosen = max(candidates, key=lambda r: (version_key(r["version"]), r["created_at"]))
                item["url"] = chosen["url"]
                item["filename"] = chosen["filename"] or item["filename"]
                item["release_id"] = chosen["id"]
                item["selected_version"] = chosen["version"]
                if not compatible_releases and build_version:
                    logger.warning(
                        "[BUILD] using out-of-tag release for %s (selected version=%s, target game=%s)",
                        item["name"], chosen["version"], build_version,
                    )

        # Last-resort persisted pointer from the catalog; use only after V2 and explicit release
        # rows fail. This field is cap-aware in prefetch, so it is safer than inventing a URL.
        for item in items:
            if item["url"] or not item.get("latest_file_url"):
                continue
            item["url"] = item["latest_file_url"]
            item["filename"] = item.get("latest_file_name") or item["filename"]
            logger.warning("[BUILD] recovered URL for %s from latest catalog pointer", item["name"])

        # Persist fallback choices for later downloads and future manifests.
        async with SessionLocal() as session:
            for item in items:
                if not item["url"]:
                    continue
                bm = await session.get(BuildMod, item["build_mod_id"])
                if bm is not None:
                    bm.selected_file_url = item["url"]
                    bm.selected_file_name = item["filename"]
                    bm.selected_version = item["selected_version"] or bm.selected_version
                    bm.release_id = item["release_id"]
            await session.commit()
        return items

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
                target_version = build.target_game_version
                build_name = build.name
            file_items = await self._resolve_build_file_items(share_id)
            missing = []
            resolved = []
            for item in file_items:
                if not item["url"]:
                    raise RuntimeError(f"Нет URL файла для {item['name']}. Проверь релизы Mod DB или выполни обновление каталога.")
                safe_filename = item["filename"] or f"{item['mod_identifier'] or item['mod_id']}.zip"
                cached = await self.cache.cached_path(item["url"], safe_filename)
                if cached is None:
                    missing.append((item["name"], item["url"], safe_filename, item["mod_id"], item["release_id"]))
                else:
                    resolved.append((cached, safe_filename, item["name"]))

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
        if not getattr(release, "file_url", None):
            continue
        try:
            tags = json.loads(release.game_versions_json or "[]")
        except (json.JSONDecodeError, TypeError):
            tags = []
        if not target or is_version_compatible([str(tag) for tag in tags], target):
            candidates.append(release)
    return max(candidates, key=lambda r: (version_key(r.mod_version), str(r.created_at or "")), default=None)


def _install_info_map(payload) -> dict[str, dict]:
    """Normalize Mod DB V2 responses, whether keyed by mod ID or returned as a row list."""
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    result: dict[str, dict] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                result[str(key).casefold()] = value
                identifier = _info_value(value, "modidstr", "modIdStr", "mod_id", "modid", "urlalias")
                if identifier:
                    result[str(identifier).casefold()] = value
    elif isinstance(data, list):
        for value in data:
            if not isinstance(value, dict):
                continue
            identifier = _info_value(value, "modidstr", "modIdStr", "mod_id", "modid", "urlalias", "identifier")
            if identifier:
                result[str(identifier).casefold()] = value
    return result


def _info_value(data: dict, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _absolute_file_url(value, moddb_client) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return urljoin(f"{moddb_client.site_base.rstrip('/')}/", text)


def _release_proxy_matches_game(release, target: str) -> bool:
    try:
        tags = json.loads(release.game_versions_json or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    return is_version_compatible([str(tag) for tag in tags], target)


def _latest_downloadable_release(releases):
    """Newest release with a file URL, regardless of game-version tags. Use only after a warning override."""
    candidates = [release for release in releases if getattr(release, "file_url", None)]
    return max(
        candidates,
        key=lambda release: (version_key(getattr(release, "mod_version", "")), str(getattr(release, "created_at", "") or "")),
        default=None,
    )
