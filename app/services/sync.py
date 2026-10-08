from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
from sqlalchemy import delete, select

from ..config import settings
from ..db import SessionLocal
from ..models import Addon, Compatibility, Dependency, DiscordSource, Mod, ModRelease
from .cache import CacheManager
from .discord_ingest import DiscordCatalogScanner, DiscordModRecord, DiscordScanResult
from .moddb import ModDBClient, ModDBData
from .parser import extract_api_relationships, extract_relationships, normalize_name
from .versioning import latest_game_version, version_key

logger = logging.getLogger(__name__)


class CatalogSyncService:
    def __init__(self, scanner: DiscordCatalogScanner, moddb: ModDBClient, cache: CacheManager | None = None, prefetch=None):
        self.scanner = scanner
        self.moddb = moddb
        self.cache = cache
        self.prefetch = prefetch
        self._run_lock = asyncio.Lock()

    async def run(self, job_id, jobs):
        async with self._run_lock:
            await self._run_locked(job_id, jobs)

    async def _run_locked(self, job_id, jobs):
        logger.info("[SYNC] started job=%s", job_id)
        scan: DiscordScanResult = await self.scanner.scan(
            lambda c, t, m: jobs.update(job_id, current=c, total=t, message=m)
        )
        records = scan.records
        errors = list(scan.errors)

        await jobs.update(
            job_id,
            current=0,
            total=max(scan.total_threads, 1),
            message=(
                f"Discord: {scan.scanned_threads}/{scan.total_threads}; "
                f"найдено модов: {len(records)}; пропущено: {scan.skipped_no_mod_link}; ошибок: {len(errors)}"
            ),
        )

        known_before = await self._load_known_mods()
        changed: list[tuple[DiscordModRecord, Mod | None]] = []
        unchanged = 0
        async with SessionLocal() as session:
            for rec in records:
                source = (
                    await session.execute(
                        select(DiscordSource).where(
                            DiscordSource.thread_id == rec.thread_id,
                            DiscordSource.message_id == rec.starter_message_id,
                        )
                    )
                ).scalar_one_or_none()
                mod = await session.get(Mod, source.mod_id) if source and source.mod_id else None
                if not mod:
                    mod = await self._find_existing_mod(session, source_url=rec.mod_db_url)
                effective_source_hash = _source_fingerprint(rec.source_hash)
                # A source changed, an algorithm changed, or this record has addon children whose
                # metadata may not have been enriched yet.
                if source and mod and source.source_hash == effective_source_hash:
                    unchanged += 1
                else:
                    changed.append((rec, mod))

        logger.info(
            "[SYNC] discord summary threads=%s records=%s changed=%s unchanged=%s skipped_no_link=%s errors=%s",
            scan.total_threads,
            len(records),
            len(changed),
            unchanged,
            scan.skipped_no_mod_link,
            len(scan.errors),
        )

        catalog_rows: list[dict] = []
        if records:
            try:
                catalog_rows = await self.moddb.get_catalog_rows(force_refresh=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Mod DB compact catalog unavailable: {type(exc).__name__}: {exc}")
                logger.exception("[SYNC] Mod DB compact catalog unavailable")

        sem = asyncio.Semaphore(max(1, settings.http_concurrency))

        async def fetch_data(url: str, fallback_title: str) -> tuple[ModDBData, str | None]:
            async with sem:
                api_error: Exception | None = None
                page_error: Exception | None = None
                try:
                    data = await self.moddb.get_mod(_moddb_identifier(url), source_url=url, catalog_rows=catalog_rows)
                except Exception as exc:  # noqa: BLE001
                    api_error = exc
                    data = ModDBData(
                        mod_db_id=None,
                        asset_id=_asset_id_from_mod_url(url),
                        mod_id=None,
                        name=fallback_title or f"Mod {_asset_id_from_mod_url(url) or ''}".strip(),
                        description=None,
                        short_description=None,
                        image_url=None,
                        author=None,
                        mod_type=None,
                        mod_db_url=url,
                        releases=[],
                        source_updated_at=None,
                        raw_json=json.dumps({"fallback": True}, ensure_ascii=False),
                        notes=[f"Mod DB client: {type(exc).__name__}: {exc}"],
                    )
                html = None
                try:
                    html = await self.moddb.get_page(url)
                except Exception as exc:  # noqa: BLE001
                    page_error = exc
                    logger.warning("[MODDB] page failed source=%s: %s: %s", url, type(exc).__name__, exc)
                if html:
                    data = self.moddb.enrich_from_page_html(data, html)
                if api_error:
                    data.notes.append(f"API/summary error: {type(api_error).__name__}: {api_error}")
                if page_error:
                    data.notes.append(f"HTML error: {type(page_error).__name__}: {page_error}")
                return data, html

        async def prepare(rec: DiscordModRecord, existing: Mod | None):
            main_data, main_html = await fetch_data(rec.mod_db_url, rec.title)
            relation_rows = []
            try:
                relation_rows.extend(extract_api_relationships(json.loads(main_data.raw_json)))
            except Exception as exc:  # noqa: BLE001
                main_data.notes.append(f"API relation parse error: {type(exc).__name__}: {exc}")
            if main_html:
                try:
                    relation_rows.extend(extract_relationships(main_html, known_before))
                except Exception as exc:  # noqa: BLE001
                    main_data.notes.append(f"HTML relation parse error: {type(exc).__name__}: {exc}")
                    logger.exception("[PARSER] relation parse failed source=%s", rec.mod_db_url)
            relation_rows = _merge_relations(relation_rows)
            image_path = await self._fetch_image(main_data)

            addon_prepared = []
            for addon in rec.addons:
                try:
                    addon_data, addon_html = await fetch_data(addon["url"], addon["title"])
                    addon_relations = []
                    try:
                        addon_relations.extend(extract_api_relationships(json.loads(addon_data.raw_json)))
                    except Exception as exc:  # noqa: BLE001
                        addon_data.notes.append(f"API relation parse error: {type(exc).__name__}: {exc}")
                    if addon_html:
                        try:
                            addon_relations.extend(extract_relationships(addon_html, known_before))
                        except Exception as exc:  # noqa: BLE001
                            addon_data.notes.append(f"HTML relation parse error: {type(exc).__name__}: {exc}")
                    addon_relations = _merge_relations(addon_relations)
                    addon_image = await self._fetch_image(addon_data)
                    addon_prepared.append((addon, addon_data, addon_image, addon_relations))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[SYNC] addon enrichment failed parent=%s addon=%s", rec.title, addon.get("url"))

            return rec, existing, main_data, image_path, relation_rows, addon_prepared

        saved = 0
        completed = 0
        save_errors: list[str] = []
        if changed:
            tasks = [asyncio.create_task(prepare(rec, mod)) for rec, mod in changed]
            for task in asyncio.as_completed(tasks):
                completed += 1
                try:
                    rec, existing, data, image_path, relations, addon_prepared = await task
                    await self._upsert_record(rec, data, image_path, relations, addon_prepared, existing)
                    saved += 1
                    logger.info(
                        "[SYNC] saved %s/%s source=%s name=%r modid=%r asset=%r api_id=%r status=%s",
                        completed,
                        len(changed),
                        rec.mod_db_url,
                        data.name,
                        data.mod_id,
                        data.asset_id,
                        data.mod_db_id,
                        "partial" if data.notes else "ok",
                    )
                except Exception as exc:  # noqa: BLE001
                    msg = f"Ошибка сохранения: {type(exc).__name__}: {exc}"
                    save_errors.append(msg)
                    logger.exception("[SYNC] %s", msg)
                await jobs.update(
                    job_id,
                    current=completed,
                    total=max(len(changed), 1),
                    message=f"Mod DB: {completed}/{len(changed)}, сохранено {saved}, ошибок {len(errors) + len(save_errors)}",
                )

        errors.extend(save_errors)
        await self._resolve_relation_targets()

        if not scan.errors:
            await self._write_registry(records)
        else:
            logger.warning("[SYNC] registry was not replaced because Discord scan was incomplete")

        final_message = (
            f"Готово: Discord {scan.scanned_threads}/{scan.total_threads}; "
            f"модов {len(records)}; обновлено {saved}; без изменений {unchanged}; "
            f"ошибок {len(errors)}"
        )
        logger.info("[SYNC] finished job=%s %s", job_id, final_message)
        await jobs.update(
            job_id,
            current=max(scan.total_threads, len(changed), 1),
            total=max(scan.total_threads, len(changed), 1),
            status="done",
            message=final_message,
            errors=errors,
        )
        if self.prefetch:
            self.prefetch.trigger()

    async def _fetch_image(self, data: ModDBData):
        if self.cache and data.image_url:
            try:
                return await self.cache.fetch_image(data.image_url, f"{data.mod_id or data.mod_db_id or data.asset_id or 'mod'}.img")
            except Exception as exc:  # noqa: BLE001
                data.notes.append(f"Preview error: {type(exc).__name__}: {exc}")
                logger.warning("[MODDB] preview failed source=%s: %s", data.mod_db_url, exc)
        return None

    async def _load_known_mods(self):
        async with SessionLocal() as session:
            mods = (await session.execute(select(Mod))).scalars().all()
            sources = (await session.execute(select(DiscordSource))).scalars().all()
            urls_by_mod: dict[int, list[str]] = {}
            for src in sources:
                if src.mod_id and src.mod_db_url:
                    urls_by_mod.setdefault(src.mod_id, []).append(src.mod_db_url)
            return [
                {
                    "id": m.id,
                    "name": m.name,
                    "mod_id": m.mod_id,
                    "mod_db_id": m.mod_db_id,
                    "mod_db_asset_id": m.mod_db_asset_id,
                    "mod_db_url": m.mod_db_url,
                    "mod_db_urls": list(dict.fromkeys(urls_by_mod.get(m.id, []))),
                }
                for m in mods
            ]

    async def _upsert_record(self, rec, data, image_path, relations, addon_prepared, existing=None):
        async with SessionLocal() as session:
            mod = await self._find_existing_mod(session, source_url=rec.mod_db_url, asset_id=data.asset_id, mod_db_id=data.mod_db_id, mod_id=data.mod_id)
            if mod is None:
                mod = existing
            if mod is None:
                mod = Mod(name=data.name or rec.title or "Неизвестный мод")
                session.add(mod)
                await session.flush()

            self._apply_mod_data(mod, data, image_path, origin="discord", parent_mod_id=None, catalog_visible=True)
            release_rows = await self._replace_releases(session, mod, data.releases)
            self._recompute_latest(mod, release_rows)

            source = (
                await session.execute(
                    select(DiscordSource).where(
                        DiscordSource.thread_id == rec.thread_id,
                        DiscordSource.message_id == rec.starter_message_id,
                    )
                )
            ).scalar_one_or_none()
            if source is None:
                source = DiscordSource(
                    thread_id=rec.thread_id,
                    message_id=rec.starter_message_id,
                    guild_id=rec.guild_id,
                    category_id=rec.category_id,
                    channel_id=rec.channel_id,
                    post_title=rec.title,
                    message_content=rec.starter_content,
                    mod_db_url=rec.mod_db_url,
                    source_hash=_source_fingerprint(rec.source_hash),
                    mod_id=mod.id,
                )
                session.add(source)
            else:
                source.mod_id = mod.id
                source.guild_id = rec.guild_id
                source.category_id = rec.category_id
                source.channel_id = rec.channel_id
                source.post_title = rec.title
                source.message_content = rec.starter_content
                source.mod_db_url = rec.mod_db_url
                source.source_hash = _source_fingerprint(rec.source_hash)
                source.scanned_at = datetime.now(timezone.utc)

            await session.execute(delete(Addon).where(Addon.mod_id == mod.id, Addon.discord_thread_id == rec.thread_id))
            for addon, addon_data, addon_image, addon_relations in addon_prepared:
                child = await self._find_existing_mod(
                    session,
                    source_url=addon_data.mod_db_url or addon["url"],
                    asset_id=addon_data.asset_id,
                    mod_db_id=addon_data.mod_db_id,
                    mod_id=addon_data.mod_id,
                )
                if child is None:
                    child = Mod(name=addon_data.name or addon["title"] or "Addon")
                    session.add(child)
                    await session.flush()
                self._apply_mod_data(child, addon_data, addon_image, origin="addon", parent_mod_id=mod.id, catalog_visible=True)
                child_release_rows = await self._replace_releases(session, child, addon_data.releases)
                self._recompute_latest(child, child_release_rows)
                session.add(Addon(
                    mod_id=mod.id,
                    title=addon_data.name or addon["title"],
                    url=addon["url"],
                    discord_message_id=addon.get("message_id"),
                    discord_thread_id=rec.thread_id,
                    source_hash=_source_fingerprint(rec.source_hash),
                    addon_mod_id=child.id,
                ))
                await self._replace_relations_for_mod(session, child, addon_relations)

            await self._replace_relations_for_mod(session, mod, relations)
            await session.commit()

    def _apply_mod_data(self, mod: Mod, data: ModDBData, image_path, *, origin: str, parent_mod_id: int | None, catalog_visible: bool):
        old_page = mod.mod_db_url
        if data.mod_db_id is not None:
            mod.mod_db_id = data.mod_db_id
        if data.asset_id is not None:
            mod.mod_db_asset_id = data.asset_id
        if data.mod_id:
            mod.mod_id = data.mod_id
        if data.name and not _bad_generated_name(data.name):
            mod.name = data.name
        mod.description = data.description or mod.description
        mod.short_description = data.short_description or mod.short_description or data.description
        if data.image_url:
            mod.image_url = data.image_url
        if image_path:
            mod.image_cache_path = str(image_path)
        mod.author = data.author or mod.author
        mod.mod_type = data.mod_type or mod.mod_type
        mod.mod_db_url = data.mod_db_url or mod.mod_db_url
        mod.refreshed_at = datetime.now(timezone.utc)
        mod.source_hash = hashlib.sha256(data.raw_json.encode()).hexdigest()
        mod.source_updated_at = data.source_updated_at
        mod.parse_status = "partial" if data.notes else "ok"
        mod.parse_message = "\n".join(data.notes)[:8000] if data.notes else None
        mod.origin = origin if mod.origin != "discord" else mod.origin
        if origin == "discord":
            mod.origin = "discord"
            mod.catalog_visible = True
            mod.parent_mod_id = None
        else:
            if mod.origin != "discord":
                mod.origin = origin
            if parent_mod_id is not None:
                mod.parent_mod_id = parent_mod_id
            mod.catalog_visible = catalog_visible
        if old_page and data.mod_db_url and old_page != data.mod_db_url:
            logger.info("[SYNC] page URL changed for %s: %s -> %s", mod.name, old_page, data.mod_db_url)

    async def _replace_releases(self, session, mod: Mod, releases):
        await session.execute(delete(ModRelease).where(ModRelease.mod_id == mod.id))
        used: set[int] = set()
        for pos, raw in enumerate(releases or []):
            release_id = _stable_release_id(raw, used, pos)
            used.add(release_id)
            tags = raw.get("tags") or raw.get("gameversions") or raw.get("game_versions") or []
            session.add(ModRelease(
                mod_id=mod.id,
                release_id=release_id,
                mod_version=str(raw.get("modversion") or ""),
                filename=raw.get("filename"),
                file_id=_int_or_none(raw.get("fileid")),
                file_url=raw.get("mainfile") or raw.get("fileurl"),
                game_versions_json=json.dumps([str(x) for x in tags]),
                created_at=_parse_dt(raw.get("created")),
                changelog=raw.get("changelog"),
                raw_json=json.dumps(raw, ensure_ascii=False),
            ))
        await session.flush()
        return (await session.execute(select(ModRelease).where(ModRelease.mod_id == mod.id))).scalars().all()

    def _recompute_latest(self, mod: Mod, releases):
        tags: set[str] = set()
        latest_release = None
        target_game = settings.max_vintage_story_version
        for release in releases:
            try:
                release_tags = json.loads(release.game_versions_json or "[]")
            except json.JSONDecodeError:
                release_tags = []
            tags.update(str(x) for x in release_tags)
            if _release_matches_branch(release_tags, target_game):
                if latest_release is None or (version_key(release.mod_version), str(release.created_at or "")) > (version_key(latest_release.mod_version), str(latest_release.created_at or "")):
                    latest_release = release
        mod.latest_game_version = latest_game_version(sorted(tags), target_game)
        mod.supported_versions_json = json.dumps(sorted(tags, key=version_key, reverse=True), ensure_ascii=False)
        mod.latest_release_id = latest_release.id if latest_release else None
        mod.latest_file_url = latest_release.file_url if latest_release else None
        mod.latest_file_name = latest_release.filename if latest_release else None

    async def _replace_relations_for_mod(self, session, mod: Mod, relations):
        await session.execute(delete(Dependency).where(Dependency.mod_id == mod.id))
        await session.execute(delete(Compatibility).where(Compatibility.mod_id == mod.id))
        mods = (await session.execute(select(Mod))).scalars().all()
        name_index = {normalize_name(m.name): m.id for m in mods if m.name}
        id_index = {normalize_name(m.mod_id): m.id for m in mods if m.mod_id}
        url_index = {str(m.mod_db_url).rstrip("/").casefold(): m.id for m in mods if m.mod_db_url}
        asset_index = {m.mod_db_asset_id: m.id for m in mods if m.mod_db_asset_id is not None}
        for relation in relations:
            target_id = None
            if relation.url:
                target_id = url_index.get(relation.url.rstrip("/").casefold())
                if target_id is None:
                    aid = _asset_id_from_mod_url(relation.url)
                    if aid is not None:
                        target_id = asset_index.get(aid)
            if target_id is None and relation.target_name:
                key = normalize_name(relation.target_name)
                target_id = name_index.get(key) or id_index.get(key)
            model = Compatibility if relation.relation_type in {"compatible", "incompatible"} else Dependency
            values = dict(
                mod_id=mod.id,
                target_mod_id=target_id,
                target_name=relation.target_name,
                target_url=relation.url,
                relation_type=relation.relation_type,
                source_kind="html",
                raw_phrase=relation.raw_phrase,
                evidence=relation.evidence,
                confidence=relation.confidence,
                verified=False,
            )
            if model is Dependency:
                values["required_version"] = None
            session.add(model(**values))

    async def _resolve_relation_targets(self):
        async with SessionLocal() as session:
            mods = (await session.execute(select(Mod))).scalars().all()
            name_index = {normalize_name(m.name): m.id for m in mods if m.name}
            id_index = {normalize_name(m.mod_id): m.id for m in mods if m.mod_id}
            url_index = {str(m.mod_db_url).rstrip("/").casefold(): m.id for m in mods if m.mod_db_url}
            asset_index = {m.mod_db_asset_id: m.id for m in mods if m.mod_db_asset_id is not None}
            for model in (Dependency, Compatibility):
                rows = (await session.execute(select(model))).scalars().all()
                for row in rows:
                    target_id = None
                    if row.target_url:
                        target_id = url_index.get(row.target_url.rstrip("/").casefold())
                        if target_id is None:
                            aid = _asset_id_from_mod_url(row.target_url)
                            if aid is not None:
                                target_id = asset_index.get(aid)
                    if target_id is None and row.target_name:
                        key = normalize_name(row.target_name)
                        target_id = name_index.get(key) or id_index.get(key)
                    if target_id and target_id != row.mod_id:
                        row.target_mod_id = target_id
            await session.commit()

    async def _find_existing_mod(self, session, source_url=None, asset_id=None, mod_db_id=None, mod_id=None):
        if asset_id is not None:
            row = (await session.execute(select(Mod).where(Mod.mod_db_asset_id == asset_id).limit(1))).scalars().first()
            if row:
                return row
        if mod_id:
            row = (await session.execute(select(Mod).where(Mod.mod_id == mod_id).limit(1))).scalars().first()
            if row:
                return row
        if mod_db_id is not None:
            row = (await session.execute(select(Mod).where(Mod.mod_db_id == mod_db_id).limit(1))).scalars().first()
            if row:
                return row
        clean = (source_url or "").rstrip("/")
        if clean:
            row = (await session.execute(select(Mod).where(Mod.mod_db_url == clean).limit(1))).scalars().first()
            if row:
                return row
            source_mod_id = (await session.execute(select(DiscordSource.mod_id).where(DiscordSource.mod_db_url == clean).limit(1))).scalars().first()
            if source_mod_id:
                return await session.get(Mod, source_mod_id)
        return None

    async def _write_registry(self, records):
        client = self.scanner.client
        channel = client.get_channel(settings.discord_registry_channel_id) if settings.discord_registry_channel_id else None
        if channel is None or not hasattr(channel, "send") or client.user is None:
            return
        manifest = {
            "schema": 4,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "records": [
                {
                    "title": r.title,
                    "url": r.mod_db_url,
                    "channel_id": r.channel_id,
                    "thread_id": r.thread_id,
                    "starter_message_id": r.starter_message_id,
                    "addons": [
                        {"title": a["title"], "url": a["url"], "message_id": a["message_id"]}
                        for a in r.addons
                    ],
                }
                for r in records
            ],
        }
        raw = json.dumps(manifest, ensure_ascii=False, indent=2)
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id == client.user.id and msg.attachments and any(att.filename == "mod-registry.txt" for att in msg.attachments):
                    await msg.delete()
                elif msg.author.id == client.user.id and msg.content.startswith("VS_MOD_REGISTRY:"):
                    await msg.delete()
            await channel.send(file=discord.File(io.BytesIO(raw.encode("utf-8")), filename="mod-registry.txt"))
            logger.info("[DISCORD] registry updated: records=%s", len(records))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[DISCORD] registry write failed: %s", exc)


def _bad_generated_name(name: str | None) -> bool:
    if not name:
        return True
    return normalize_name(name) in {"disclaimer", "mod info", "description", "files", "mods"}


def _release_matches_branch(tags, target: str) -> bool:
    wanted = [int(x) for x in __import__("re").findall(r"\d+", target)[:2]]
    if len(wanted) != 2:
        return False
    for tag in tags:
        nums = [int(x) for x in __import__("re").findall(r"\d+", str(tag))]
        if len(nums) >= 2 and nums[:2] == wanted:
            return True
    return False


def _stable_release_id(raw, used: set[int], pos: int) -> int:
    for key in ("releaseid", "releaseId", "id"):
        value = _int_or_none(raw.get(key)) if isinstance(raw, dict) else None
        if value is not None and value not in used:
            return value
    seed = int(hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12], 16)
    candidate = max(1, seed % 2_000_000_000)
    while candidate in used:
        candidate += 1
    return candidate


def _moddb_identifier(url: str) -> str:
    parts = url.rstrip("/").split("/")
    if "show" in parts and "mod" in parts:
        try:
            return parts[parts.index("mod") + 1]
        except (ValueError, IndexError):
            pass
    return parts[-1]


def _asset_id_from_mod_url(url: str | None) -> int | None:
    if not url:
        return None
    import re
    from urllib.parse import urlparse
    path = urlparse(url).path.rstrip("/")
    match = re.fullmatch(r"/show/mod/(\d+)", path, re.I)
    return int(match.group(1)) if match else None


def _merge_relations(relations):
    merged = {}
    for rel in relations:
        key = (
            rel.relation_type,
            (rel.url or "").rstrip("/").casefold() if rel.url else None,
            normalize_name(rel.target_name or "") if not rel.url else None,
        )
        current = merged.get(key)
        if current is None or rel.confidence > current.confidence:
            merged[key] = rel
    return list(merged.values())


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _source_fingerprint(raw_hash: str) -> str:
    return hashlib.sha256(f"{settings.catalog_source_version}:{raw_hash}".encode()).hexdigest()
