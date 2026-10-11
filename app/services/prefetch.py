from __future__ import annotations

import asyncio
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Dependency, DependencyRequest, Mod, ModRelease
from .cache import CacheManager
from .moddb import ModDBClient
from .modinfo import dependencies_from_modinfo, read_modinfo
from .identity import canonical_identifier, is_core_mod_identifier, is_library_dependency
from .priority import PriorityCoordinator
from .versioning import is_release_compatible_with_cap, is_version_compatible, latest_game_version, version_key

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
    def __init__(self, cache: CacheManager, moddb: ModDBClient, coordinator: PriorityCoordinator, discord_client=None):
        self.cache = cache
        self.moddb = moddb
        self.coordinator = coordinator
        self.discord_client = discord_client
        self._dependency_locks: dict[str, asyncio.Lock] = {}
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
            # Clean dependency-only content rows created by pre-v0.9 behavior before making
            # the queue. Only recognized libraries remain eligible for automatic downloads.
            await self._prune_legacy_dependency_imports()
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
            if total == 0:
                logger.info("[PREFETCH] no downloadable releases matched the configured game-version cap")
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
                    try:
                        cached_before = await self.cache.cached_path(
                            item.file_url, item.filename, expected_release_id=item.release_id
                        )

                        # Only the network-capable cache operation occupies a background slot.
                        # Do not keep the slot while processing local modinfo/dependencies: those
                        # dependency lookups may need to yield to an arriving user request.
                        async with self.coordinator.background_slot():
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
                        # Reading modinfo.json is local and cheap. Do it on every prefetch pass,
                        # not only after a new HTTP download, so exact 100% dependency relations
                        # are restored even after an application restart.
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
                        logger.exception("[PREFETCH] file fetch/dependency processing failed: %s", item.mod_identifier)
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
                    try:
                        await self.cache.cached_path(
                            item.file_url, item.filename, expected_release_id=item.release_id
                        )
                        # Only the actual file fetch uses background priority. Local ZIP parsing
                        # and DB writes must happen after releasing the slot, because resolving a
                        # dependency may itself need a low-priority HTTP request.
                        async with self.coordinator.background_slot():
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

    async def _prune_legacy_dependency_imports(self) -> None:
        """Stop downloading content mods imported by older versions as dependencies.

        Older builds automatically created a catalog row for every modinfo identifier. That made
        content mods and community-specific fixes look like libraries, and caused repeated
        dependency rows on subsequent runs. Keep positive library matches; redirect old edges to
        a curated Discord-title alias when one exists; otherwise turn them into unresolved edges
        and ask the user to curate the dependency in Discord.
        """
        requests: list[tuple[str, str, str, str | None, str, str]] = []
        alias_resolutions: list[tuple[str, str]] = []
        to_purge: list[int] = []
        changed = 0
        async with SessionLocal() as session:
            legacy_rows = (await session.execute(
                select(Mod).where(Mod.origin == "dependency").order_by(Mod.id)
            )).scalars().all()
            curated_rows = (await session.execute(
                select(Mod).where(Mod.origin == "discord", Mod.catalog_visible.is_(True))
            )).scalars().all()

            def id_from_edge(edge, fallback: str) -> str:
                evidence = str(edge.evidence or "")
                prefix = "modinfo.json dependencies:"
                if evidence.casefold().startswith(prefix.casefold()):
                    value = evidence[len(prefix):].strip().split("=", 1)[0].strip()
                    if value:
                        return value
                return fallback

            for legacy in legacy_rows:
                dep_id = str(legacy.mod_id or "").strip()
                if not dep_id:
                    continue
                dep_key = canonical_identifier(legacy.canonical_id or dep_id)
                if is_core_mod_identifier(dep_id):
                    legacy.catalog_visible = False
                    legacy.origin = "core_dependency"
                    await session.execute(delete(Dependency).where(
                        (Dependency.target_mod_id == legacy.id) | (Dependency.mod_id == legacy.id)
                    ))
                    changed += 1
                    continue

                if is_library_dependency(
                    dep_id,
                    name=legacy.name,
                    description=legacy.short_description or legacy.description,
                    mod_type=legacy.mod_type,
                    configured_ids=settings.auto_library_ids,
                ):
                    # These are the only dependency-origin rows eligible for automatic download.
                    if not legacy.catalog_visible:
                        legacy.catalog_visible = True
                        changed += 1
                    continue

                aliases = [m for m in curated_rows if canonical_identifier(m.canonical_id) == dep_key]
                if len(aliases) > 1:
                    logger.error(
                        "[PREFETCH] ambiguous forum title alias for legacy dependency %s: %s",
                        dep_id, [m.id for m in aliases],
                    )
                    aliases = []
                alias = aliases[0] if aliases else None
                if alias is not None:
                    alias_resolutions.append((dep_key, alias.name))
                incoming = (await session.execute(select(Dependency).where(
                    Dependency.target_mod_id == legacy.id,
                    Dependency.relation_type == "dependency",
                ))).scalars().all()

                for edge in incoming:
                    source_mod = await session.get(Mod, edge.mod_id)
                    source_name = source_mod.name if source_mod else f"mod #{edge.mod_id}"
                    actual_dep_id = id_from_edge(edge, dep_id)
                    actual_key = canonical_identifier(actual_dep_id)
                    if alias is not None:
                        conflict = (await session.execute(select(Dependency).where(
                            Dependency.mod_id == edge.mod_id,
                            Dependency.target_mod_id == alias.id,
                            Dependency.relation_type == edge.relation_type,
                            Dependency.id != edge.id,
                        ).limit(1))).scalars().first()
                        if conflict:
                            conflict.required_version = conflict.required_version or edge.required_version
                            conflict.confidence = max(float(conflict.confidence or 0), float(edge.confidence or 0))
                            conflict.verified = bool(conflict.verified or edge.verified)
                            await session.delete(edge)
                        else:
                            edge.target_mod_id = alias.id
                            edge.target_name = alias.name
                            edge.target_url = alias.mod_db_url
                    else:
                        edge.target_mod_id = None
                        edge.target_name = legacy.name or actual_dep_id
                        edge.target_url = legacy.mod_db_url
                        requests.append((
                            actual_dep_id, actual_key, legacy.name or actual_dep_id,
                            legacy.mod_db_url, source_name, edge.required_version or "",
                        ))

                # This row is no longer a catalog item. Its historical releases/build references
                # stay in SQLite for old builds, but it cannot be selected or downloaded by prefetch.
                await session.execute(delete(Dependency).where(Dependency.mod_id == legacy.id))
                legacy.catalog_visible = False
                legacy.origin = "superseded_dependency" if alias is not None else "unresolved_dependency"
                to_purge.append(legacy.id)
                changed += 1
                logger.warning(
                    "[PREFETCH] disabled legacy non-library dependency row %s (%s)%s",
                    legacy.name, dep_id, f"; mapped to Discord entry {alias.name!r}" if alias else "; requesting manual Discord entry",
                )
            if changed:
                await session.commit()

        # Old versions cached some of these non-library dependencies automatically. Remove those
        # archives so they do not keep consuming disk space or masquerade as preloaded content.
        # Historical builds retain release metadata and can re-download an old file on demand.
        for mod_id in to_purge:
            async with self.coordinator.background_slot():
                await self.cache.purge_mod_files(mod_id)

        # Registry messages are sent after committing so Discord latency never holds a DB transaction.
        unique_requests: dict[tuple[str, str], tuple[str, str, str, str | None, str, str]] = {}
        for request in requests:
            unique_requests.setdefault((request[1], request[4]), request)
        for request in unique_requests.values():
            await self._register_dependency_request(*request)
        for dep_key, alias_name in alias_resolutions:
            await self._resolve_dependency_request(dep_key, alias_name)
        if changed:
            logger.info(
                "[PREFETCH] legacy dependency cleanup complete: %s rows hidden, %s manual requests refreshed",
                changed, len(unique_requests),
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
            payload = read_modinfo(path)
            deps = dependencies_from_modinfo(payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PREFETCH] modinfo unavailable for %s: %s", path.name, exc)
            return

        await self._refresh_cached_release_metadata(item, payload)
        if not deps:
            return

        logger.info(
            "[PREFETCH] modinfo parsed %s: %s",
            item.mod_identifier,
            ", ".join(f"{dep} >= {version}" if version else dep for dep, version in sorted(deps.items())),
        )
        for dep_id, required_version in deps.items():
            await self._upsert_exact_dependency(item, dep_id, required_version)

        logger.info(
            "[PREFETCH] modinfo dependencies saved %s: %s",
            item.mod_identifier,
            ", ".join(sorted(deps)),
        )

    async def _refresh_cached_release_metadata(self, item: PrefetchItem, payload: dict) -> None:
        """Use modinfo.json's own version as the installed-archive truth.

        The V2 install endpoint can select a file whose filename/version does not line up exactly
        with an API release row. Without this reconciliation the cache could hold 0.4.0 while the
        resolver still warned that the only available release was 0.3.2.
        """
        version = str(payload.get("version") or "").strip() or None
        # Even mod archives without a modinfo version can still teach us the V2-selected file URL
        # and filename for an already-known release.
        if not version and not (item.file_url or item.filename):
            return
        async with SessionLocal() as session:
            mod = await session.get(Mod, item.mod_id)
            if mod is None:
                return
            release = await session.get(ModRelease, item.release_id) if item.release_id else None
            if release is not None and release.mod_id != mod.id:
                release = None
            if release is None:
                release = (await session.execute(
                    select(ModRelease).where(
                        ModRelease.mod_id == mod.id,
                        or_(ModRelease.file_url == item.file_url, ModRelease.filename == item.filename),
                    ).order_by(ModRelease.id.desc()).limit(1)
                )).scalars().first()
            if release is None:
                if not version:
                    return
                existing_ids = set((await session.execute(
                    select(ModRelease.release_id).where(ModRelease.mod_id == mod.id)
                )).scalars().all())
                import hashlib
                candidate_id = max(1, int(hashlib.sha256(item.file_url.encode()).hexdigest()[:8], 16))
                while candidate_id in existing_ids:
                    candidate_id = 1 if candidate_id >= 2_000_000_000 else candidate_id + 1
                release = ModRelease(
                    mod_id=mod.id,
                    release_id=candidate_id,
                    mod_version=version,
                    filename=item.filename,
                    file_url=item.file_url,
                    game_versions_json=json.dumps([item.game_version]),
                    raw_json=json.dumps({"source": "v2-install-information+modinfo", "version": version}, ensure_ascii=False),
                )
                session.add(release)
                await session.flush()
            else:
                if version and release.mod_version != version:
                    logger.info(
                        "[PREFETCH] corrected release version from archive: %s %r -> %r (%s)",
                        item.mod_identifier, release.mod_version, version, item.filename,
                    )
                    release.mod_version = version
                # Fill missing fields even when the version string already matches. Some V1
                # release rows omit mainfile/file URL while V2 install-information supplied a
                # working URL; previously this branch left the release permanently unusable.
                if item.filename and release.filename != item.filename:
                    release.filename = item.filename
                if item.file_url and release.file_url != item.file_url:
                    release.file_url = item.file_url
                try:
                    tags = json.loads(release.game_versions_json or "[]")
                except (json.JSONDecodeError, TypeError):
                    tags = []
                if item.game_version and not is_version_compatible([str(tag) for tag in tags], item.game_version):
                    tags.append(item.game_version)
                    release.game_versions_json = json.dumps(list(dict.fromkeys(str(tag) for tag in tags)), ensure_ascii=False)
            releases = (await session.execute(
                select(ModRelease).where(ModRelease.mod_id == mod.id)
            )).scalars().all()
            _recompute_mod_latest(mod, releases, settings.max_vintage_story_version)
            await session.commit()

    async def _find_dependency_target(self, dep_id: str) -> Mod | None:
        dep_key = canonical_identifier(dep_id)
        dep_exact = str(dep_id or "").strip().casefold()
        async with SessionLocal() as session:
            candidates = (await session.execute(
                select(Mod).where(
                    Mod.catalog_visible.is_(True),
                    or_(
                        func.lower(Mod.mod_id) == dep_exact,
                        func.lower(Mod.canonical_id) == dep_key,
                    ),
                ).order_by(Mod.id)
            )).scalars().all()
        if not candidates:
            return None
        # A deliberate Discord title is an override: a forum post titled "Ancient Armory" can
        # point at a 1.22 fix whose actual modinfo ID is different. Prefer that curated alias over
        # a stale automatically discovered dependency-only copy of the original mod.
        candidates.sort(key=lambda mod: (
            0 if mod.origin == "discord" and canonical_identifier(mod.canonical_id) == dep_key else 1,
            0 if mod.origin != "dependency" else 1,
            0 if str(mod.mod_id or "").casefold() == dep_exact else 1,
            mod.id,
        ))
        # If multiple curated posts claim the same canonical identifier, avoid guessing.
        top = candidates[0]
        same_rank = [m for m in candidates if (
            m.origin == "discord" and canonical_identifier(m.canonical_id) == dep_key
        ) == (top.origin == "discord" and canonical_identifier(top.canonical_id) == dep_key)]
        if top.origin == "discord" and canonical_identifier(top.canonical_id) == dep_key and len(same_rank) > 1:
            logger.error("[PREFETCH] ambiguous Discord canonical dependency ID %s maps to mod rows %s", dep_id, [m.id for m in same_rank])
            return None
        return top

    async def _upsert_exact_dependency(self, source: PrefetchItem, dep_id: str, required_version: str):
        # `game`, `survival`, and `creative` are core game components shipped with Vintage Story,
        # not third-party downloads. Do not catalog them as ordinary mods or show missing-release warnings.
        if is_core_mod_identifier(dep_id):
            logger.debug("[PREFETCH] ignored core dependency %s for %s", dep_id, source.mod_identifier)
            return
        dep_key = canonical_identifier(dep_id)
        if not dep_key:
            return
        lock = self._dependency_locks.setdefault(dep_key, asyncio.Lock())
        async with lock:
            await self._upsert_exact_dependency_locked(source, dep_id, required_version, dep_key)

    async def _upsert_exact_dependency_locked(
        self, source: PrefetchItem, dep_id: str, required_version: str, dep_key: str
    ):
        async with SessionLocal() as session:
            source_mod = await session.get(Mod, source.mod_id)
            source_name = source_mod.name if source_mod else source.mod_identifier
        if source_mod is None:
            return

        target = await self._find_dependency_target(dep_id)
        data = None
        known_library = is_library_dependency(
            dep_id, configured_ids=settings.auto_library_ids
        )
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
                    "[PREFETCH] exact dependency metadata failed %s -> %s: %s",
                    source.mod_identifier, dep_id, exc,
                )
                if known_library:
                    target = await self._upsert_library_record(dep_id, dep_key, None)
                else:
                    await self._upsert_dependency_edge(
                        source.mod_id, dep_id, required_version, None,
                        target_name=dep_id, target_url=None,
                    )
                    await self._register_dependency_request(
                        dep_id, dep_key, dep_id, None, source_name, required_version
                    )
                    return

            if target is None and data is not None:
                is_library = is_library_dependency(
                    dep_id,
                    name=data.name,
                    description=data.short_description or data.description,
                    mod_type=data.mod_type,
                    configured_ids=settings.auto_library_ids,
                )
                if is_library:
                    target = await self._upsert_library_record(dep_id, dep_key, data)
                else:
                    display_name = data.name or dep_id
                    logger.warning(
                        "[PREFETCH] manual Discord catalog entry required: dependency=%s (%s), required_by=%s",
                        dep_id, display_name, source_name,
                    )
                    await self._upsert_dependency_edge(
                        source.mod_id, dep_id, required_version, None,
                        target_name=display_name, target_url=data.mod_db_url,
                    )
                    await self._register_dependency_request(
                        dep_id, dep_key, display_name, data.mod_db_url, source_name, required_version
                    )
                    return

        if target is None:
            await self._upsert_dependency_edge(
                source.mod_id, dep_id, required_version, None,
                target_name=(data.name if data else None) or dep_id,
                target_url=(data.mod_db_url if data else None),
            )
            return

        await self._resolve_dependency_request(dep_key, target.name)
        await self._upsert_dependency_edge(
            source.mod_id, dep_id, required_version, target.id,
            target_name=target.name, target_url=target.mod_db_url,
        )

    async def _upsert_library_record(self, dep_id: str, dep_key: str, data) -> Mod | None:
        async with SessionLocal() as session:
            target = (await session.execute(select(Mod).where(
                Mod.catalog_visible.is_(True),
                or_(
                    func.lower(Mod.mod_id) == str(dep_id).casefold(),
                    func.lower(Mod.canonical_id) == dep_key,
                ),
            ).order_by(Mod.id).limit(1))).scalars().first()
            if target is None:
                target = Mod(
                    mod_db_id=data.mod_db_id if data else None,
                    mod_db_asset_id=data.asset_id if data else None,
                    mod_id=(data.mod_id if data and data.mod_id else dep_id),
                    canonical_id=dep_key,
                    name=(data.name if data else None) or dep_id,
                    description=data.description if data else None,
                    short_description=(data.short_description if data else None) or (data.description if data else None),
                    image_url=data.image_url if data else None,
                    author=data.author if data else None,
                    mod_type=data.mod_type if data else "library",
                    mod_db_url=(data.mod_db_url if data else None) or f"{self.moddb.site_base}/{dep_id}",
                    catalog_visible=True,
                    origin="dependency",
                    parse_status="partial" if data is None or (data and data.notes) else "ok",
                    parse_message=("Mod DB lookup unavailable; retry on next prefetch" if data is None else "\n".join(data.notes)[:8000] if data.notes else None),
                )
                session.add(target)
                await session.flush()
                if data and data.releases:
                    await _replace_dependency_releases(session, target, data.releases)
            elif target.origin in {"dependency", "unresolved_dependency"} and data is not None:
                target.origin = "dependency"
                target.catalog_visible = True
                target.canonical_id = target.canonical_id or dep_key
                if data.name:
                    target.name = data.name
                target.description = data.description or target.description
                target.short_description = data.short_description or target.short_description or data.description
                target.image_url = data.image_url or target.image_url
                target.author = data.author or target.author
                target.mod_type = data.mod_type or target.mod_type
                target.mod_db_id = data.mod_db_id or target.mod_db_id
                target.mod_db_asset_id = data.asset_id or target.mod_db_asset_id
                target.mod_db_url = data.mod_db_url or target.mod_db_url
                if data.releases:
                    await _replace_dependency_releases(session, target, data.releases)
            # Never read target.releases as an ORM relationship in async code; query them explicitly.
            target_id = target.id
            releases = (await session.execute(select(ModRelease).where(ModRelease.mod_id == target_id))).scalars().all()
            _recompute_mod_latest(target, releases, settings.max_vintage_story_version)
            await session.commit()
        return await self._find_dependency_target(dep_id)

    async def _upsert_dependency_edge(
        self, source_mod_id: int, dep_id: str, required_version: str, target_mod_id: int | None,
        *, target_name: str | None, target_url: str | None,
    ):
        dep_key = canonical_identifier(dep_id)
        async with SessionLocal() as session:
            # NULL target IDs do not collide under SQLite UNIQUE constraints; match unresolved
            # rows by their exact modinfo identifier to prevent a new relation on every restart.
            rows = (await session.execute(select(Dependency).where(
                Dependency.mod_id == source_mod_id,
                Dependency.relation_type == "dependency",
            ))).scalars().all()
            def edge_identifier(row):
                evidence = str(row.evidence or "")
                prefix = "modinfo.json dependencies:"
                if evidence.casefold().startswith(prefix.casefold()):
                    identifier_part = evidence[len(prefix):].strip().split("=", 1)[0].strip()
                    return canonical_identifier(identifier_part)
                return ""

            matches = [row for row in rows if (
                (target_mod_id is not None and row.target_mod_id == target_mod_id) or
                (row.target_mod_id is None and (
                    canonical_identifier(row.target_name) == dep_key or edge_identifier(row) == dep_key
                ))
            )]
            # Prefer an already-resolved edge, otherwise upgrade the first matching unresolved
            # row. Remove older duplicates left by previous versions (nullable target IDs are not
            # protected by SQLite's normal UNIQUE constraint).
            existing = next((row for row in matches if row.target_mod_id == target_mod_id and target_mod_id is not None), None)
            if existing is None and matches:
                existing = matches[0]
            for duplicate in matches:
                if duplicate is not existing:
                    await session.delete(duplicate)
            evidence = f"modinfo.json dependencies: {dep_id}={required_version}"
            if existing is None:
                existing = Dependency(
                    mod_id=source_mod_id,
                    target_mod_id=target_mod_id,
                    target_name=target_name,
                    target_url=target_url,
                    relation_type="dependency",
                    required_version=required_version or None,
                    source_kind="modinfo",
                    raw_phrase="dependencies",
                    evidence=evidence,
                    confidence=1.0,
                    verified=True,
                )
                session.add(existing)
            else:
                existing.target_mod_id = target_mod_id
                existing.target_name = target_name or existing.target_name
                existing.target_url = target_url or existing.target_url
                existing.required_version = required_version or None
                existing.source_kind = "modinfo"
                existing.confidence = 1.0
                existing.verified = True
                existing.evidence = evidence
                existing.raw_phrase = "dependencies"
            await session.commit()

    async def flush_open_dependency_requests(self) -> None:
        """Send persisted dependency requests after the Discord Gateway becomes ready."""
        async with SessionLocal() as session:
            rows = (await session.execute(select(DependencyRequest).where(
                DependencyRequest.status == "open",
                DependencyRequest.discord_message_id.is_(None),
            ).order_by(DependencyRequest.id))).scalars().all()
            pending = [
                (r.mod_identifier, r.requested_name, r.target_url, r.required_by_json)
                for r in rows
            ]
        for dep_key, requested_name, target_url, required_by_json in pending:
            try:
                required_by = json.loads(required_by_json or "[]")
            except json.JSONDecodeError:
                required_by = []
            first = required_by[0] if required_by else {"name": "unknown mod", "version": ""}
            await self._register_dependency_request(
                dep_key, dep_key, requested_name, target_url,
                str(first.get("name") or "unknown mod"), str(first.get("version") or ""),
            )

    async def _register_dependency_request(
        self, dep_id: str, dep_key: str, requested_name: str, target_url: str | None,
        source_name: str, required_version: str,
    ):
        async with SessionLocal() as session:
            request = (await session.execute(select(DependencyRequest).where(
                DependencyRequest.mod_identifier == dep_key
            ))).scalars().first()
            required_by = []
            if request:
                try:
                    required_by = json.loads(request.required_by_json or "[]")
                except json.JSONDecodeError:
                    required_by = []
            entry = {"name": source_name, "version": required_version or ""}
            if entry not in required_by:
                required_by.append(entry)
            if request is None:
                request = DependencyRequest(
                    mod_identifier=dep_key,
                    requested_name=requested_name or dep_id,
                    target_url=target_url,
                    required_by_json=json.dumps(required_by, ensure_ascii=False),
                    status="open",
                )
                session.add(request)
                await session.flush()
            else:
                request.requested_name = requested_name or request.requested_name
                request.target_url = target_url or request.target_url
                request.required_by_json = json.dumps(required_by, ensure_ascii=False)
                request.status = "open"
            request_id = request.id
            message_id = request.discord_message_id
            await session.commit()

        if message_id or not settings.discord_registry_channel_id or not self.discord_client or not self.discord_client.is_ready():
            return
        channel = self.discord_client.get_channel(settings.discord_registry_channel_id)
        if channel is None:
            try:
                channel = await self.discord_client.fetch_channel(settings.discord_registry_channel_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[PREFETCH] cannot open registry channel for dependency request %s: %s", dep_id, exc)
                return
        if not hasattr(channel, "send"):
            logger.warning("[PREFETCH] registry channel does not allow messages for dependency request %s", dep_id)
            return
        req_lines = [entry["name"] + (f" (requires {entry['version']})" if entry.get("version") else "") for entry in required_by]
        canonical_title = (requested_name or dep_id).strip()
        content = (
            f"📌 **Нужно добавить зависимость в каталог модов**\n"
            f"Название: **{requested_name or dep_id}**\n"
            f"`modinfo` ID: `{dep_id}`\n"
            f"Требуют: {', '.join(req_lines[:12]) or 'неизвестно'}\n"
            f"\nДобавьте форумный пост в один из настроенных каналов модов. **Заголовок поста должен быть `{canonical_title}`** — это имя будет использоваться для сопоставления зависимости. Ссылку можно дать на актуальный фикс/форк, даже если его внутренний modid отличается. После «Обновить» программа сопоставит зависимость по заголовку."
        )
        if target_url and target_url.startswith(("http://", "https://")):
            content += f"\nСтраница Mod DB: {target_url}"
        try:
            message = await channel.send(content[:1900])
            async with SessionLocal() as session:
                request = await session.get(DependencyRequest, request_id)
                if request and not request.discord_message_id:
                    request.discord_message_id = message.id
                    await session.commit()
            logger.info("[PREFETCH] requested manual Discord entry for %s (%s)", dep_id, requested_name)
        except Exception:
            logger.exception("[PREFETCH] failed to post dependency request for %s", dep_id)

    async def _resolve_dependency_request(self, dep_key: str, resolved_name: str):
        async with SessionLocal() as session:
            request = (await session.execute(select(DependencyRequest).where(
                DependencyRequest.mod_identifier == dep_key
            ))).scalars().first()
            if request and request.status != "resolved":
                request.status = "resolved"
                request.requested_name = resolved_name or request.requested_name
                await session.commit()
                logger.info("[PREFETCH] manual dependency request resolved: %s -> %s", dep_key, resolved_name)


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
    """Return whether the catalog already has a usable release for ``cap``.

    ``_release_matches_cap`` accepts a ModRelease object, so keep the object
    intact here. The previous implementation decoded ``game_versions_json``
    into a list and then passed that list to ``_release_matches_cap``; this
    caused the background prefetch worker to crash before it ever reached the
    download queue.
    """
    for release in mod.releases:
        if release.file_url and _release_matches_cap(release, cap):
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
