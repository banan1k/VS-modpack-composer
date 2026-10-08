from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from datetime import datetime, timezone

import discord
from sqlalchemy import delete, select

from ..config import settings
from ..db import SessionLocal
from ..models import Addon, Compatibility, Dependency, DiscordSource, Mod, ModRelease
from .cache import CacheManager
from .discord_ingest import DiscordCatalogScanner, DiscordModRecord, DiscordScanResult
from .moddb import ModDBClient, ModDBData
from .parser import extract_api_relationships, extract_relationships, normalize_name
from .versioning import latest_game_version

logger = logging.getLogger(__name__)


class CatalogSyncService:
    def __init__(self, scanner: DiscordCatalogScanner, moddb: ModDBClient, cache: CacheManager | None = None):
        self.scanner = scanner
        self.moddb = moddb
        self.cache = cache
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
                f"найдено модов: {len(records)}; ошибок: {len(errors)}"
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

        if scan.errors:
            # Never reconcile/de-register old Discord sources after a partial scan.
            logger.warning("[SYNC] Discord scan incomplete; stale sources will be kept")

        catalog_rows: list[dict] = []
        if changed:
            try:
                catalog_rows = await self.moddb.get_catalog_rows(force_refresh=True)
            except Exception as exc:  # noqa: BLE001
                msg = f"Mod DB compact catalog unavailable: {type(exc).__name__}: {exc}"
                errors.append(msg)
                logger.exception("[SYNC] %s", msg)

        sem = asyncio.Semaphore(max(1, settings.http_concurrency))

        async def prepare(rec: DiscordModRecord, existing: Mod | None):
            async with sem:
                api_error: Exception | None = None
                page_error: Exception | None = None
                data: ModDBData
                html: str | None = None

                try:
                    data = await self.moddb.get_mod(
                        _moddb_identifier(rec.mod_db_url),
                        source_url=rec.mod_db_url,
                        catalog_rows=catalog_rows,
                    )
                except Exception as exc:  # noqa: BLE001
                    api_error = exc
                    data = ModDBData(
                        mod_db_id=None,
                        asset_id=_asset_id_from_mod_url(rec.mod_db_url),
                        mod_id=None,
                        name=rec.title or f"Mod {_asset_id_from_mod_url(rec.mod_db_url) or ''}".strip(),
                        description=None,
                        short_description=None,
                        image_url=None,
                        author=None,
                        mod_type=None,
                        mod_db_url=rec.mod_db_url,
                        releases=[],
                        source_updated_at=None,
                        raw_json=json.dumps({"fallback": True}, ensure_ascii=False),
                        notes=[f"Mod DB client: {type(exc).__name__}: {exc}"],
                    )
                    logger.exception("[MODDB] client failed source=%s", rec.mod_db_url)

                try:
                    html = await self.moddb.get_page(rec.mod_db_url)
                except Exception as exc:  # noqa: BLE001
                    page_error = exc
                    logger.warning(
                        "[MODDB] page failed source=%s: %s: %s",
                        rec.mod_db_url,
                        type(exc).__name__,
                        exc,
                    )

                if html:
                    data = self.moddb.enrich_from_page_html(data, html)

                if api_error:
                    data.notes.append(f"API/summary error: {type(api_error).__name__}: {api_error}")
                if page_error:
                    data.notes.append(f"HTML error: {type(page_error).__name__}: {page_error}")

                relations = []
                if data.raw_json:
                    try:
                        relations.extend(extract_api_relationships(json.loads(data.raw_json)))
                    except Exception as exc:  # noqa: BLE001
                        data.notes.append(f"API relation parse error: {type(exc).__name__}: {exc}")
                if html:
                    try:
                        relations.extend(extract_relationships(html, known_before))
                    except Exception as exc:  # noqa: BLE001
                        data.notes.append(f"HTML relation parse error: {type(exc).__name__}: {exc}")
                        logger.exception("[PARSER] relation parse failed source=%s", rec.mod_db_url)

                relations = _merge_relations(relations)

                image_path = None
                if self.cache and data.image_url:
                    try:
                        image_path = await self.cache.fetch_image(
                            data.image_url,
                            f"{data.mod_id or data.mod_db_id or data.asset_id or rec.thread_id}.img",
                        )
                    except Exception as exc:  # noqa: BLE001
                        data.notes.append(f"Preview error: {type(exc).__name__}: {exc}")
                        logger.warning("[MODDB] preview failed source=%s: %s", rec.mod_db_url, exc)

                return rec, existing, data, image_path, relations

        saved = 0
        completed = 0
        save_errors: list[str] = []
        if changed:
            tasks = [asyncio.create_task(prepare(rec, mod)) for rec, mod in changed]
            for task in asyncio.as_completed(tasks):
                completed += 1
                try:
                    rec, existing, data, image_path, relations = await task
                    await self._upsert_record(rec, data, image_path, relations, known_before, existing)
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
                    total=len(changed),
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

    async def _upsert_record(
        self,
        rec: DiscordModRecord,
        data: ModDBData,
        image_path,
        relations,
        known_mods,
        existing: Mod | None = None,
    ):
        async with SessionLocal() as session:
            # Re-resolve identity using the freshly fetched page/API data first. This repairs
            # old rows that were created with the wrong API ID or wrong Mod DB page mapping.
            mod = await self._find_existing_mod(
                session,
                source_url=rec.mod_db_url,
                asset_id=data.asset_id,
                mod_db_id=data.mod_db_id,
                mod_id=data.mod_id,
            )
            if mod is None:
                mod = existing
            if mod is None:
                mod = Mod(name=data.name or rec.title or "Неизвестный мод")
                session.add(mod)
                await session.flush()

            old_image_url = mod.image_url
            mod.mod_db_id = data.mod_db_id
            mod.mod_db_asset_id = data.asset_id
            mod.mod_id = data.mod_id or mod.mod_id
            mod.name = data.name or rec.title or mod.name
            mod.description = data.description or mod.description
            mod.short_description = data.short_description or mod.short_description or data.description
            mod.image_url = data.image_url or mod.image_url
            mod.image_cache_path = str(image_path) if image_path else mod.image_cache_path
            mod.author = data.author or mod.author
            mod.mod_type = data.mod_type or mod.mod_type
            mod.mod_db_url = rec.mod_db_url or data.mod_db_url
            mod.refreshed_at = datetime.now(timezone.utc)
            mod.source_hash = hashlib.sha256(data.raw_json.encode()).hexdigest()
            mod.source_updated_at = data.source_updated_at
            mod.parse_status = "partial" if data.notes else "ok"
            mod.parse_message = "\n".join(data.notes)[:8000] if data.notes else None

            # No compiled build archive is persisted here. Image cache entries are keyed by URL,
            # so a corrected image URL automatically receives a new file. The old file is harmless.
            if old_image_url and data.image_url and old_image_url != data.image_url:
                logger.info("[SYNC] preview URL changed for %s: %s -> %s", mod.name, old_image_url, data.image_url)

            await session.execute(delete(ModRelease).where(ModRelease.mod_id == mod.id))
            all_versions: set[str] = set()
            latest_release = None
            used_release_ids: set[int] = set()
            for pos, raw in enumerate(data.releases):
                tags = raw.get("tags") or raw.get("gameversions") or raw.get("game_versions") or []
                tags = [str(x) for x in tags]
                all_versions.update(tags)
                release_id = _stable_release_id(raw, used_release_ids, pos)
                used_release_ids.add(release_id)
                session.add(
                    ModRelease(
                        mod_id=mod.id,
                        release_id=release_id,
                        mod_version=str(raw.get("modversion") or ""),
                        filename=raw.get("filename"),
                        file_id=_int_or_none(raw.get("fileid")),
                        file_url=raw.get("mainfile") or raw.get("fileurl"),
                        game_versions_json=json.dumps(tags),
                        created_at=_parse_dt(raw.get("created")),
                        changelog=raw.get("changelog"),
                        raw_json=json.dumps(raw, ensure_ascii=False),
                    )
                )
                candidate_key = (_version_key(str(raw.get("modversion") or "0")), str(raw.get("created", "")))
                if latest_release is None or candidate_key > latest_release[0]:
                    latest_release = (candidate_key, raw)

            mod.latest_game_version = latest_game_version(sorted(all_versions))
            if latest_release:
                raw = latest_release[1]
                mod.latest_release_id = int(raw.get("releaseid") or 0)
                mod.latest_file_url = raw.get("mainfile") or raw.get("fileurl")
                mod.latest_file_name = raw.get("filename")
            mod.supported_versions_json = json.dumps(
                sorted(all_versions, key=_version_key, reverse=True), ensure_ascii=False
            )

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

            # Add-ons belong to a Discord thread. Do not erase add-ons found in another source thread
            # for the same mod.
            await session.execute(
                delete(Addon).where(
                    Addon.mod_id == mod.id,
                    Addon.discord_thread_id == rec.thread_id,
                )
            )
            for addon in rec.addons:
                session.add(
                    Addon(
                        mod_id=mod.id,
                        title=addon["title"],
                        url=addon["url"],
                        discord_message_id=addon["message_id"],
                        discord_thread_id=rec.thread_id,
                        source_hash=_source_fingerprint(rec.source_hash),
                    )
                )

            await session.execute(delete(Dependency).where(Dependency.mod_id == mod.id))
            await session.execute(delete(Compatibility).where(Compatibility.mod_id == mod.id))

            # Exact current DB indexes. Relation targets are also resolved in a final pass after all
            # changed mods have been saved, so links to mods processed later are safe.
            mods = (await session.execute(select(Mod))).scalars().all()
            name_index = {normalize_name(m.name): m.id for m in mods if m.name}
            id_index = {normalize_name(m.mod_id): m.id for m in mods if m.mod_id}
            url_index = {str(m.mod_db_url).rstrip("/").casefold(): m.id for m in mods if m.mod_db_url}
            asset_index = {m.mod_db_asset_id: m.id for m in mods if m.mod_db_asset_id is not None}
            dbid_index = {m.mod_db_id: m.id for m in mods if m.mod_db_id is not None}
            url_index[str(rec.mod_db_url).rstrip("/").casefold()] = mod.id
            if mod.mod_db_asset_id is not None:
                asset_index[mod.mod_db_asset_id] = mod.id
            if mod.mod_db_id is not None:
                dbid_index[mod.mod_db_id] = mod.id
            if mod.mod_id:
                id_index[normalize_name(mod.mod_id)] = mod.id
            if mod.name:
                name_index[normalize_name(mod.name)] = mod.id

            for relation in relations:
                target_id = None
                if relation.url:
                    clean = relation.url.rstrip("/").casefold()
                    target_id = url_index.get(clean)
                    if target_id is None:
                        asset_id = _asset_id_from_mod_url(relation.url)
                        if asset_id is not None:
                            target_id = asset_index.get(asset_id)
                if target_id is None and relation.target_name:
                    target_key = normalize_name(relation.target_name)
                    target_id = name_index.get(target_key) or id_index.get(target_key)
                model = Compatibility if relation.relation_type in {"compatible", "incompatible"} else Dependency
                session.add(
                    model(
                        mod_id=mod.id,
                        target_mod_id=target_id,
                        target_name=relation.target_name,
                        target_url=relation.url,
                        relation_type=relation.relation_type,
                        raw_phrase=relation.raw_phrase,
                        evidence=relation.evidence,
                        confidence=relation.confidence,
                        verified=False,
                    )
                )
            await session.commit()

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
                            asset_id = _asset_id_from_mod_url(row.target_url)
                            if asset_id is not None:
                                target_id = asset_index.get(asset_id)
                    if target_id is None and row.target_name:
                        key = normalize_name(row.target_name)
                        target_id = name_index.get(key) or id_index.get(key)
                    if target_id and target_id != row.mod_id:
                        row.target_mod_id = target_id
            await session.commit()

    async def _find_existing_mod(
        self,
        session,
        source_url: str | None = None,
        asset_id: int | None = None,
        mod_db_id: int | None = None,
        mod_id: str | None = None,
    ):
        # Prefer intrinsic Mod DB/game identity over a stale source-to-mod association.
        if asset_id is not None:
            row = (await session.execute(select(Mod).where(Mod.mod_db_asset_id == asset_id).limit(1))).scalars().first()
            if row:
                return row
        if mod_db_id is not None:
            row = (await session.execute(select(Mod).where(Mod.mod_db_id == mod_db_id).limit(1))).scalars().first()
            if row:
                return row
        if mod_id:
            row = (await session.execute(select(Mod).where(Mod.mod_id == mod_id).limit(1))).scalars().first()
            if row:
                return row
            normalized = normalize_name(mod_id)
            mods = (await session.execute(select(Mod))).scalars().all()
            for candidate in mods:
                if candidate.mod_id and normalize_name(candidate.mod_id) == normalized:
                    return candidate
        clean = (source_url or "").rstrip("/")
        if clean:
            row = (await session.execute(select(Mod).where(Mod.mod_db_url == clean).limit(1))).scalars().first()
            if row:
                return row
            source_mod_id = (
                await session.execute(
                    select(DiscordSource.mod_id).where(DiscordSource.mod_db_url == clean).limit(1)
                )
            ).scalars().first()
            if source_mod_id:
                row = await session.get(Mod, source_mod_id)
                if row:
                    return row
        return None

    async def _write_registry(self, records):
        client = self.scanner.client
        channel = client.get_channel(settings.discord_registry_channel_id) if settings.discord_registry_channel_id else None
        if channel is None or not hasattr(channel, "send") or client.user is None:
            return
        manifest = {
            "schema": 3,
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
                if msg.author.id == client.user.id and msg.attachments and any(
                    att.filename == "mod-registry.txt" for att in msg.attachments
                ):
                    await msg.delete()
                elif msg.author.id == client.user.id and msg.content.startswith("VS_MOD_REGISTRY:"):
                    await msg.delete()
            await channel.send(file=discord.File(io.BytesIO(raw.encode("utf-8")), filename="mod-registry.txt"))
            logger.info("[DISCORD] registry updated: records=%s", len(records))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[DISCORD] registry write failed: %s", exc)


def _moddb_identifier(url: str) -> str:
    parts = url.rstrip("/").split("/")
    if "show" in parts and "mod" in parts:
        try:
            return parts[parts.index("mod") + 1]
        except (ValueError, IndexError):
            pass
    return parts[-1]


def _asset_id_from_mod_url(url: str | None) -> int | None:
    import re
    from urllib.parse import urlparse
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    match = re.fullmatch(r"/show/mod/(\d+)", path, re.I)
    return int(match.group(1)) if match else None



def _merge_relations(relations):
    """Merge API + HTML detections so a relation cannot violate the DB uniqueness constraint."""
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
        elif current and len(rel.evidence or "") > len(current.evidence or ""):
            current.evidence = rel.evidence
    return list(merged.values())

def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

def _source_fingerprint(raw_hash: str) -> str:
    """Bind Discord source hashes to the catalog parser/sync version.

    This makes a catalog algorithm upgrade a one-time repair pass: existing
    Discord sources are reprocessed once even when the Discord messages did not
    change. After that, identical sources remain incremental as before.
    """
    payload = f"catalog-source-v{settings.catalog_source_version}:{raw_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_release_id(raw: dict, used: set[int], position: int) -> int:
    """Return a deterministic per-mod release ID even for malformed API rows."""
    rid = _int_or_none(raw.get("releaseid"))
    if rid is not None and rid > 0 and rid not in used:
        return rid
    file_id = _int_or_none(raw.get("fileid"))
    candidate = -file_id if file_id and file_id > 0 else -(position + 1)
    while candidate in used:
        candidate -= 1
    return candidate

def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _version_key(value: str):
    import re
    text = str(value or "")
    raw_nums = [int(x) for x in re.findall(r"\d+", text)[:4]]
    nums = tuple((raw_nums + [0, 0, 0, 0])[:4])
    stable = 1 if not re.search(r"(?:pre|rc|alpha|beta|dev)", text, re.I) else 0
    # Fixed tuple shape prevents Python from ever comparing ints with strings
    # when Mod DB returns versions such as `1.22`, `1.22.7` and `1.22.7-pre`.
    return nums + (stable, text.lower())
