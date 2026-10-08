from __future__ import annotations

import asyncio
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Dependency, Mod, ModRelease
from .cache import CacheManager
from .moddb import ModDBClient
from .modinfo import read_dependencies
from .priority import PriorityCoordinator
from .versioning import is_release_compatible_with_cap, latest_game_version, version_key

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], Awaitable[None]]


@dataclass(frozen=True)
class PrefetchItem:
    mod_id: int
    mod_identifier: str
    game_version: str
    file_url: str
    filename: str
    release_id: int | None
    mod_version: str | None


class PrefetchManager:
    def __init__(self, cache: CacheManager, moddb: ModDBClient, coordinator: PriorityCoordinator):
        self.cache = cache
        self.moddb = moddb
        self.coordinator = coordinator
        self._trigger = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()
        self._status_lock = asyncio.Lock()
        self._status = {
            "running": False,
            "current": 0,
            "total": 0,
            "errors": 0,
            "game_version": None,
            "message": "Ожидание",
        }

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="vs-mod-prefetch")

    def stop(self):
        self._stop.set()
        self._trigger.set()

    def trigger(self):
        self._trigger.set()

    async def wait_stopped(self):
        if self._task:
            await self._task

    async def status(self) -> dict:
        async with self._status_lock:
            return dict(self._status)

    async def _set_status(self, **updates):
        async with self._status_lock:
            self._status.update(updates)

    async def _loop(self):
        # Resume from the existing catalog on startup, then wait for future catalog syncs.
        self._trigger.set()
        while not self._stop.is_set():
            await self._trigger.wait()
            self._trigger.clear()
            if self._stop.is_set():
                break
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[PREFETCH] background run failed")
                await self._set_status(running=False, message="Ошибка фоновой загрузки")

    async def run_once(self):
        async with self._run_lock:
            target_game = settings.max_vintage_story_version
            await self._set_status(
                running=True,
                current=0,
                total=0,
                errors=0,
                game_version=target_game,
                message=f"Предзагрузка модов для Vintage Story {target_game}",
            )
            async with SessionLocal() as session:
                mods = (
                    await session.execute(
                        select(Mod)
                        .options(selectinload(Mod.releases))
                        .where(Mod.mod_id.is_not(None), Mod.catalog_visible.is_(True))
                        .order_by(Mod.id)
                    )
                ).scalars().all()
            if not mods:
                logger.info("[PREFETCH] nothing to prefetch")
                await self._set_status(running=False, message="Нет модов для предзагрузки")
                return

            resolver: dict[str, dict] = {}
            mod_identifiers = [m.mod_id for m in mods if m.mod_id]
            for batch_start in range(0, len(mod_identifiers), 60):
                batch = mod_identifiers[batch_start: batch_start + 60]
                try:
                    async with self.coordinator.background_slot():
                        response = await self.moddb.get_install_information(batch, target_game)
                    data = response.get("data", response) if isinstance(response, dict) else {}
                    if isinstance(data, dict):
                        resolver.update({str(k).casefold(): v for k, v in data.items() if isinstance(v, dict)})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[PREFETCH] install-information failed for batch %s: %s", batch_start, exc)

            # If V2 explicitly says there is no installable release for the configured game
            # ceiling, remove an older cached archive. This keeps the cache aligned with the
            # current MAX_VINTAGE_STORY_VERSION rather than silently retaining an unusable file.
            for mod in mods:
                info = resolver.get((mod.mod_id or '').casefold()) if mod.mod_id else None
                if isinstance(info, dict) and info.get('errorCode') and not _has_local_release_for_cap(mod, target_game):
                    await self.cache.purge_mod_files(mod.id)
                    logger.info('[PREFETCH] purged cache for %s: no release compatible with %s', mod.mod_id, target_game)

            queue = self._make_queue(mods, resolver, target_game)
            total = len(queue)
            await self._set_status(total=total, message=f"Кеширование: 0/{total}")
            logger.info("[PREFETCH] queued %s files for game=%s", total, target_game)
            if not queue:
                await self._set_status(running=False, current=0, total=0, message="Нет подходящих файлов")
                return

            sem = asyncio.Semaphore(max(1, settings.prefetch_concurrency))
            counter = 0
            error_count = 0
            counter_lock = asyncio.Lock()

            async def one(item: PrefetchItem):
                nonlocal error_count
                nonlocal counter
                async with sem:
                    if self._stop.is_set():
                        return
                    async with self.coordinator.background_slot():
                        try:
                            cached_before = await self.cache.cached_path(
                                item.file_url, item.filename, expected_release_id=item.release_id
                            )
                            path = await self.cache.fetch_file(
                                item.file_url,
                                item.filename,
                                owner_mod_id=item.mod_id,
                                owner_release_id=item.release_id,
                                game_version=item.game_version,
                                expected_release_id=item.release_id,
                                replace_stale=True,
                            )
                            await self.cache.purge_obsolete_mod_files(item.mod_id, item.file_url)
                            # Reading modinfo.json is local and cheap. Do it on every prefetch
                            # pass, not only after a new HTTP download, so exact 100% dependency
                            # relations are restored even after an application restart.
                            await self._process_modinfo(item, path)
                            refreshed = cached_before is None or cached_before != path
                            logger.debug(
                                "[PREFETCH] cache %s %s release=%s",
                                "refreshed" if refreshed else "hit",
                                item.mod_identifier,
                                item.mod_version,
                            )
                        except Exception:
                            async with counter_lock:
                                error_count += 1
                                current_errors = error_count
                            await self._set_status(errors=current_errors)
                            logger.exception("[PREFETCH] file fetch failed: %s", item.mod_identifier)
                            return
                async with counter_lock:
                    counter += 1
                    current = counter
                await self._set_status(current=current, total=total, message=f"Кеширование: {current}/{total}")
                logger.info("[PREFETCH] %s/%s %s", current, total, item.filename)

            results = await asyncio.gather(*(one(item) for item in queue), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("[PREFETCH] task error: %s", result)

            logger.info("[PREFETCH] completed %s/%s", counter, total)

            # modinfo.json can discover previously unknown dependencies. Process dependency mods
            # until a pass no longer adds a new installable dependency.
            processed_mod_ids = {i.mod_id for i in queue}
            for _pass in range(3):
                async with SessionLocal() as session:
                    dep_mods = (
                        await session.execute(
                            select(Mod)
                            .options(selectinload(Mod.releases))
                            .where(Mod.mod_id.is_not(None))
                            .order_by(Mod.id)
                        )
                    ).scalars().all()
                extra_mods = [m for m in dep_mods if m.id not in processed_mod_ids]
                if not extra_mods:
                    break
                ids = [m.mod_id for m in extra_mods if m.mod_id]
                extra_resolver: dict[str, dict] = {}
                for batch_start in range(0, len(ids), 60):
                    batch = ids[batch_start: batch_start + 60]
                    try:
                        async with self.coordinator.background_slot():
                            response = await self.moddb.get_install_information(batch, target_game)
                        data = response.get("data", response) if isinstance(response, dict) else {}
                        if isinstance(data, dict):
                            extra_resolver.update({str(k).casefold(): v for k, v in data.items() if isinstance(v, dict)})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[PREFETCH] dependency resolver pass failed: %s", exc)
                extra_queue = self._make_queue(extra_mods, extra_resolver, target_game)
                if not extra_queue:
                    break
                logger.info("[PREFETCH] discovered %s additional dependency files", len(extra_queue))
                await self._set_status(total=total + len(extra_queue), message=f"Зависимости: {counter}/{total + len(extra_queue)}")
                for item in extra_queue:
                    if self._stop.is_set():
                        break
                    async with self.coordinator.background_slot():
                        try:
                            await self.cache.cached_path(
                                item.file_url, item.filename, expected_release_id=item.release_id
                            )
                            path = await self.cache.fetch_file(
                                item.file_url,
                                item.filename,
                                owner_mod_id=item.mod_id,
                                owner_release_id=item.release_id,
                                game_version=item.game_version,
                                expected_release_id=item.release_id,
                                replace_stale=True,
                            )
                            await self.cache.purge_obsolete_mod_files(item.mod_id, item.file_url)
                            await self._process_modinfo(item, path)
                            counter += 1
                            processed_mod_ids.add(item.mod_id)
                            await self._set_status(current=counter, total=max(total, counter), message=f"Зависимости: {counter}/{max(total, counter)}")
                        except Exception:
                            async with counter_lock:
                                error_count += 1
                            await self._set_status(errors=error_count)
                            logger.exception("[PREFETCH] additional dependency fetch failed: %s", item.mod_identifier)

            await self._set_status(
                running=False,
                current=counter,
                total=max(total, counter),
                errors=error_count,
                message=(
                    "Предзагрузка завершена"
                    if error_count == 0
                    else f"Предзагрузка завершена с ошибками: {error_count}"
                ),
            )

    def _make_queue(self, mods: list[Mod], resolver: dict[str, dict], game_version: str) -> list[PrefetchItem]:
        out: list[PrefetchItem] = []
        for mod in mods:
            if not mod.mod_id:
                continue
            info = resolver.get(mod.mod_id.casefold()) or {}
            url = _absolute_url(info.get("fileUrl"), self.moddb.site_base)
            filename = str(info.get("fileName") or "").strip()
            release_id = None
            mod_version = None
            for release in mod.releases:
                if filename and release.filename == filename:
                    if _release_matches_cap(release, game_version):
                        release_id = release.id
                        mod_version = release.mod_version
                        break
            if not url:
                # Safe local fallback: never pick a release above the configured game-version ceiling.
                candidates = []
                for release in mod.releases:
                    if _release_matches_cap(release, game_version):
                        candidates.append(release)
                release = max(
                    candidates,
                    key=lambda r: (version_key(r.mod_version), r.created_at or ""),
                    default=None,
                )
                if release:
                    url = _absolute_url(release.file_url, self.moddb.site_base)
                    filename = release.filename or f"{mod.mod_id}.zip"
                    release_id = release.id
                    mod_version = release.mod_version
            if not url:
                logger.warning("[PREFETCH] no installable file for %s (%s)", mod.name, mod.mod_id)
                continue
            out.append(
                PrefetchItem(
                    mod.id,
                    mod.mod_id,
                    game_version,
                    url,
                    filename or f"{mod.mod_id}.zip",
                    release_id,
                    mod_version,
                )
            )
        return out

    async def _process_modinfo(self, item: PrefetchItem, path: Path):
        try:
            deps = read_dependencies(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PREFETCH] modinfo unavailable for %s: %s", path.name, exc)
            return
        if not deps:
            return

        for dep_id, required_version in deps.items():
            await self._upsert_exact_dependency(item, dep_id, required_version)

        logger.info(
            "[PREFETCH] modinfo dependencies %s: %s",
            item.mod_identifier,
            ", ".join(sorted(deps)),
        )

    async def _upsert_exact_dependency(self, source: PrefetchItem, dep_id: str, required_version: str):
        dep_norm = dep_id.casefold()
        async with SessionLocal() as session:
            source_mod = await session.get(Mod, source.mod_id)
            target = (
                await session.execute(
                    select(Mod).where(func.lower(Mod.mod_id) == dep_norm).limit(1)
                )
            ).scalars().first() if source_mod else None
        if source_mod is None:
            return

        if target is None:
            try:
                async with self.coordinator.background_slot():
                    rows = await self.moddb.get_catalog_rows()
                    data = await self.moddb.get_mod(
                        dep_id,
                        source_url=f"{self.moddb.site_base}/{dep_id}",
                        catalog_rows=rows,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[PREFETCH] exact dependency resolve failed %s -> %s: %s",
                    source.mod_identifier,
                    dep_id,
                    exc,
                )
                return
            async with SessionLocal() as session:
                target = (
                    await session.execute(
                        select(Mod).where(func.lower(Mod.mod_id) == dep_norm).limit(1)
                    )
                ).scalars().first()
                if target is None:
                    target = Mod(
                        mod_db_id=data.mod_db_id,
                        mod_db_asset_id=data.asset_id,
                        mod_id=data.mod_id or dep_id,
                        name=data.name or dep_id,
                        description=data.description,
                        short_description=data.short_description,
                        image_url=data.image_url,
                        author=data.author,
                        mod_type=data.mod_type,
                        mod_db_url=data.mod_db_url,
                        catalog_visible=True,
                        origin="dependency",
                        parse_status="partial" if data.notes else "ok",
                        parse_message="\n".join(data.notes)[:8000] if data.notes else None,
                    )
                    session.add(target)
                    await session.flush()
                    await _replace_dependency_releases(session, target, data.releases)
                    _recompute_mod_latest(target, list(target.releases), settings.max_vintage_story_version)
                    await session.commit()
                    logger.info("[PREFETCH] discovered dependency mod %s", dep_id)
                else:
                    target.catalog_visible = True
                    if target.origin != "discord":
                        target.origin = "dependency"
                    if data.name:
                        target.name = data.name
                    target.description = data.description or target.description
                    target.short_description = data.short_description or target.short_description or data.description
                    target.image_url = data.image_url or target.image_url
                    target.mod_db_id = data.mod_db_id or target.mod_db_id
                    target.mod_db_asset_id = data.asset_id or target.mod_db_asset_id
                    target.mod_db_url = data.mod_db_url or target.mod_db_url
                    if data.releases:
                        await _replace_dependency_releases(session, target, data.releases)
                        target_releases = (
                            await session.execute(select(ModRelease).where(ModRelease.mod_id == target.id))
                        ).scalars().all()
                        _recompute_mod_latest(target, target_releases, settings.max_vintage_story_version)
                    await session.commit()

        async with SessionLocal() as session:
            target = (
                await session.execute(
                    select(Mod).where(func.lower(Mod.mod_id) == dep_norm).limit(1)
                )
            ).scalars().first()
            if not target:
                return
            existing = (
                await session.execute(
                    select(Dependency).where(
                        Dependency.mod_id == source.mod_id,
                        Dependency.target_mod_id == target.id,
                        Dependency.relation_type == "dependency",
                    )
                )
            ).scalars().first()
            evidence = f"modinfo.json dependencies: {dep_id}={required_version}"
            if existing:
                existing.required_version = required_version or None
                existing.source_kind = "modinfo"
                existing.confidence = 1.0
                existing.verified = True
                existing.target_name = target.name
                existing.target_url = target.mod_db_url
                existing.evidence = evidence
                existing.raw_phrase = "dependencies"
            else:
                session.add(
                    Dependency(
                        mod_id=source.mod_id,
                        target_mod_id=target.id,
                        target_name=target.name,
                        target_url=target.mod_db_url,
                        relation_type="dependency",
                        required_version=required_version or None,
                        source_kind="modinfo",
                        raw_phrase="dependencies",
                        evidence=evidence,
                        confidence=1.0,
                        verified=True,
                    )
                )
            await session.commit()


async def _replace_dependency_releases(session, mod: Mod, releases: list[dict]):
    from hashlib import sha256

    await session.execute(delete(ModRelease).where(ModRelease.mod_id == mod.id))
    used: set[int] = set()
    for pos, raw in enumerate(releases or []):
        release_id = None
        for key in ("releaseid", "releaseId", "id"):
            try:
                value = int(raw.get(key)) if raw.get(key) is not None else None
            except (TypeError, ValueError):
                value = None
            if value is not None and value not in used:
                release_id = value
                break
        if release_id is None:
            seed = int(sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12], 16)
            release_id = max(1, seed % 2_000_000_000)
            while release_id in used:
                release_id += 1
        used.add(release_id)
        tags = raw.get("tags") or raw.get("gameversions") or raw.get("game_versions") or []
        session.add(
            ModRelease(
                mod_id=mod.id,
                release_id=release_id,
                mod_version=str(raw.get("modversion") or ""),
                filename=raw.get("filename"),
                file_id=_safe_int(raw.get("fileid"), None),
                file_url=raw.get("mainfile") or raw.get("fileurl"),
                game_versions_json=json.dumps([str(x) for x in tags]),
                raw_json=json.dumps(raw, ensure_ascii=False),
            )
        )
    await session.flush()


def _recompute_mod_latest(mod: Mod, releases: list[ModRelease], cap: str):
    tags: set[str] = set()
    latest = None
    for release in releases:
        try:
            release_tags = json.loads(release.game_versions_json or "[]")
        except json.JSONDecodeError:
            release_tags = []
        tags.update(str(x) for x in release_tags)
        if is_release_compatible_with_cap(release_tags, cap):
            if latest is None or (version_key(release.mod_version), str(release.created_at or "")) > (
                version_key(latest.mod_version),
                str(latest.created_at or ""),
            ):
                latest = release
    mod.latest_game_version = latest_game_version(sorted(tags), cap)
    mod.supported_versions_json = json.dumps(sorted(tags, key=version_key, reverse=True), ensure_ascii=False)
    mod.latest_release_id = latest.id if latest else None
    mod.latest_file_url = latest.file_url if latest else None
    mod.latest_file_name = latest.filename if latest else None



def _has_local_release_for_cap(mod: Mod, cap: str) -> bool:
    for release in mod.releases:
        try:
            tags = json.loads(release.game_versions_json or "[]")
        except json.JSONDecodeError:
            tags = []
        if _release_matches_cap(tags, cap) and release.file_url:
            return True
    return False


def _release_matches_cap(release: ModRelease, cap: str) -> bool:
    try:
        tags = json.loads(release.game_versions_json or "[]")
    except json.JSONDecodeError:
        tags = []
    return is_release_compatible_with_cap(tags, cap)


def _absolute_url(url: str | None, base: str) -> str | None:
    if not url:
        return None
    from urllib.parse import urljoin

    return urljoin(base.rstrip("/") + "/", str(url))


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
