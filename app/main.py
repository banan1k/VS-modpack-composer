from __future__ import annotations

import asyncio
import io
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .config import settings
from .db import SessionLocal, init_db
from .models import Addon, Build, BuildMod, Compatibility, Dependency, DependencyRequest, Mod, ModRelease, DiscordSource
from .services.builds import BuildService
from .services.cache import CacheManager
from .services.discord_ingest import DiscordCatalogScanner
from .services.jobs import JobManager
from .services.moddb import ModDBClient
from .services.prefetch import PrefetchManager
from .services.priority import PriorityCoordinator
from .services.sync import CatalogSyncService

settings.ensure_dirs()


def _configure_logging():
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    root = logging.getLogger()
    if not root.handlers:
        root.setLevel(logging.INFO)
        root.addHandler(console_handler)
        root.addHandler(file_handler)
    else:
        # Always add our persistent file handler once, while preserving uvicorn's console setup.
        if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
            root.addHandler(file_handler)


_configure_logging()
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
bot = discord.Client(intents=intents)
discord_task: asyncio.Task | None = None


@bot.event
async def on_ready():
    logger.info("[Discord] Connected as %s (guilds: %s)", bot.user, len(bot.guilds))
    # Deliver durable dependency requests created during startup, without rerunning every ZIP.
    asyncio.create_task(prefetch.flush_open_dependency_requests(), name="flush-dependency-requests")


jobs = JobManager()
publish_locks: dict[str, asyncio.Lock] = {}

moddb = ModDBClient(
    settings.moddb_api_base_url,
    settings.moddb_base_url,
    settings.http_timeout_seconds,
)
cache = CacheManager(
    settings.file_cache_dir,
    settings.image_cache_dir,
    settings.http_timeout_seconds,
    settings.download_concurrency,
)
priority = PriorityCoordinator()
prefetch = PrefetchManager(cache, moddb, priority, discord_client=bot)
builds = BuildService(cache, moddb, priority)


class SyncResponse(BaseModel):
    job_id: str


class BuildCreate(BaseModel):
    mod_ids: list[int] = Field(min_length=1)
    target_game_version: str | None = None
    name: str | None = None
    force: bool = False


class BuildPublish(BaseModel):
    publish: bool


app = FastAPI(title=settings.app_name)


async def _start_discord():
    try:
        await bot.start(settings.discord_token)
    except discord.PrivilegedIntentsRequired:
        logger.error(
            "[Discord] Message Content Intent is disabled in the Discord Developer Portal. "
            "Enable it under Bot -> Privileged Gateway Intents."
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[Discord] Bot stopped with error: %s", exc)


@app.on_event("startup")
async def startup():
    global discord_task
    await init_db()
    await cache.migrate_cache_layout()
    prefetch.start()
    if settings.discord_token:
        discord_task = asyncio.create_task(_start_discord())


@app.on_event("shutdown")
async def shutdown():
    prefetch.stop()
    await prefetch.wait_stopped()
    await moddb.close()
    await cache.close()
    if not bot.is_closed():
        await bot.close()


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(Path(__file__).parent / "web" / "index.html")


@app.get("/static/{name}", include_in_schema=False)
async def static_file(name: str):
    file = Path(__file__).parent / "web" / name
    if not file.exists():
        raise HTTPException(404)
    return FileResponse(file)


@app.get("/api/prefetch/status")
async def prefetch_status():
    return await prefetch.status()


@app.get("/api/catalog")
async def catalog():
    async with SessionLocal() as session:
        mods = (await session.execute(select(Mod).where(Mod.catalog_visible.is_(True)).order_by(Mod.name))).scalars().all()
        mod_ids = [m.id for m in mods]
        deps = (await session.execute(select(Dependency).where(Dependency.mod_id.in_(mod_ids)))).scalars().all() if mod_ids else []
        threshold = settings.relation_min_confidence
        required: dict[int, list[int]] = {}
        for d in deps:
            if d.relation_type == "dependency" and d.confidence >= threshold and d.target_mod_id:
                required.setdefault(d.mod_id, []).append(d.target_mod_id)
        return [
            {**serialize_mod(m), "required_dependency_ids": required.get(m.id, [])}
            for m in mods
        ]


@app.get("/api/mods/{mod_id}/relations")
async def relations(mod_id: int):
    async with SessionLocal() as session:
        deps = (
            await session.execute(select(Dependency).where(Dependency.mod_id == mod_id))
        ).scalars().all()
        comps = (
            await session.execute(select(Compatibility).where(Compatibility.mod_id == mod_id))
        ).scalars().all()
        addons = (
            await session.execute(select(Addon).where(Addon.mod_id == mod_id).order_by(Addon.id))
        ).scalars().all()
        target_ids = {x.target_mod_id for x in deps + comps if x.target_mod_id}
        target_ids.update(a.addon_mod_id for a in addons if a.addon_mod_id)
        targets = {}
        if target_ids:
            targets = {
                m.id: m
                for m in (
                    await session.execute(select(Mod).where(Mod.id.in_(target_ids)))
                ).scalars().all()
            }
        return {
            "dependencies": [serialize_relation(x, targets) for x in deps],
            "compatibility": [serialize_relation(x, targets) for x in comps],
            "addons": [
                {
                    "relation_type": "addon",
                    "target_mod_id": a.addon_mod_id,
                    "target_name": targets.get(a.addon_mod_id).name if a.addon_mod_id and a.addon_mod_id in targets else a.title,
                    "target_url": targets.get(a.addon_mod_id).mod_db_url if a.addon_mod_id and a.addon_mod_id in targets else a.url,
                    "confidence": 1.0,
                    "verified": True,
                    "evidence": "Discord thread",
                    "raw_phrase": "addon",
                    "source_kind": "discord",
                    "required_version": None,
                }
                for a in addons
            ],
        }


@app.post("/api/sync", response_model=SyncResponse)
async def sync_catalog():
    if not settings.discord_token:
        raise HTTPException(503, "Discord is not configured")
    if not settings.forum_channel_ids:
        raise HTTPException(503, "DISCORD_FORUM_CHANNEL_IDS is not configured")
    if not bot.is_ready():
        raise HTTPException(503, "Discord bot is not connected yet. Check the console and try again.")
    # Only one catalog sync may run at a time. A second click no longer creates a concurrent
    # scan that competes for SQLite writes and can make the catalog appear to lose records.
    for task_id, task in list(jobs.tasks.items()):
        if not task.done():
            job = await jobs.get(task_id)
            if job and job.kind == "catalog_sync":
                return {"job_id": task_id}
    scanner = DiscordCatalogScanner(bot)
    service = CatalogSyncService(scanner, moddb, cache, prefetch)

    async def worker(job_id, jm):
        async with priority.user_priority():
            await service.run(job_id, jm)

    job_id = await jobs.create("catalog_sync", worker)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = await jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "current": job.current,
        "total": job.total,
        "message": job.message,
        "errors": json.loads(job.errors_json or "[]"),
    }


@app.post("/api/builds/inspect")
async def inspect_build(data: BuildCreate):
    return await builds.inspect(data.mod_ids, data.target_game_version)


@app.post("/api/builds")
async def create_build(data: BuildCreate):
    result = await builds.inspect(data.mod_ids, data.target_game_version)
    if not result["ok"] and not data.force:
        return JSONResponse(status_code=409, content=result)
    build = await builds.create(data.mod_ids, data.target_game_version, data.name)
    return {
        "share_id": build.share_id,
        "name": build.name,
        "mod_count": len(result.get("resolved_mod_ids", [])),
        "auto_added_mod_ids": result.get("auto_added_mod_ids", []),
        "url": f"{settings.public_base_url.rstrip('/')}/build/{build.share_id}",
        "published": False,
    }


@app.get("/api/builds/{share_id}")
async def get_build(share_id: str):
    async with SessionLocal() as session:
        build_id = (await session.execute(
            select(Build.id).where(Build.share_id == share_id)
        )).scalar_one_or_none()
    # Don't keep a read session open during a Discord network request/recovery write.
    if build_id is None:
        await _recover_build_from_discord(share_id)
    async with SessionLocal() as session:
        build = (await session.execute(
            select(Build).where(Build.share_id == share_id)
        )).scalar_one_or_none()
        if not build:
            raise HTTPException(404, "Сборка не найдена локально и не удалось восстановить её из Discord.")
        items = (await session.execute(
            select(BuildMod, Mod, ModRelease)
            .join(Mod, Mod.id == BuildMod.mod_id)
            .outerjoin(ModRelease, ModRelease.id == BuildMod.release_id)
            .where(BuildMod.build_id == build.id)
            .order_by(BuildMod.position)
        )).all()
        return {
            "share_id": share_id,
            "name": build.name,
            "target_game_version": build.target_game_version,
            "published": build.published,
            "mods": [
                {
                    "id": m.id,
                    "name": m.name,
                    "selected_version": bm.selected_version,
                    "auto_added": bm.auto_added,
                    "image_url": m.image_url,
                    "mod_db_url": m.mod_db_url,
                }
                for bm, m, _r in items
            ],
        }


@app.post("/api/builds/{share_id}/publish")
async def publish_build(share_id: str, data: BuildPublish):
    if not data.publish:
        return {"published": False}
    if not settings.discord_builds_channel_id or not bot.is_ready():
        raise HTTPException(503, "Discord publishing is not configured or bot is offline")

    lock = publish_locks.setdefault(share_id, asyncio.Lock())
    async with lock:
        async with SessionLocal() as session:
            build = (await session.execute(select(Build).where(Build.share_id == share_id))).scalar_one_or_none()
            if not build:
                raise HTTPException(404)
            url = f"{settings.public_base_url.rstrip('/')}/build/{share_id}"
            if build.published:
                return {"published": True, "url": url, "message_id": build.discord_message_id, "already_published": True}
            items = (
                await session.execute(
                    select(BuildMod, Mod)
                    .join(Mod, Mod.id == BuildMod.mod_id)
                    .where(BuildMod.build_id == build.id)
                    .order_by(BuildMod.position)
                )
            ).all()
            build_name = build.name
            target_version = build.target_game_version
            mod_names = [m.name for _bm, m in items]
            first_image_url = next((m.image_url for _bm, m in items if m.image_url), None)

        channel = bot.get_channel(settings.discord_builds_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise HTTPException(503, "Builds channel is unavailable")

        # Crash-recovery/idempotency: if the bot already sent this share URL but the DB update
        # did not make it to disk, adopt that message instead of publishing a duplicate.
        try:
            async for previous in channel.history(limit=50):
                if bot.user is None or previous.author.id != bot.user.id:
                    continue
                if previous.embeds and any((embed.url or "") == url for embed in previous.embeds):
                    async with SessionLocal() as session:
                        build = (await session.execute(select(Build).where(Build.share_id == share_id))).scalar_one()
                        build.published = True
                        build.discord_message_id = previous.id
                        await session.commit()
                    return {"published": True, "url": url, "message_id": previous.id, "already_published": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Discord] duplicate-publish recovery check failed for %s: %s", share_id, exc)

        # Publish a single machine-readable manifest. Historical messages that contain modpack.txt
        # remain recoverable, but new posts no longer clutter the builds channel with text files.
        async with SessionLocal() as session:
            manifest_rows = (await session.execute(
                select(BuildMod, Mod, ModRelease)
                .join(Mod, Mod.id == BuildMod.mod_id)
                .outerjoin(ModRelease, ModRelease.id == BuildMod.release_id)
                .where(BuildMod.build_id == build.id)
                .order_by(BuildMod.position)
            )).all()
            manifest_mods = [
                {
                    "name": mod.name,
                    "mod_id": mod.mod_id,
                    "canonical_id": mod.canonical_id,
                    "mod_db_url": mod.mod_db_url,
                    "mod_db_asset_id": mod.mod_db_asset_id,
                    "mod_db_id": mod.mod_db_id,
                    "selected_version": bm.selected_version or (release.mod_version if release else None),
                    "filename": bm.selected_file_name or (release.filename if release else None) or mod.latest_file_name,
                    "selected_file_url": bm.selected_file_url or (release.file_url if release else None) or mod.latest_file_url,
                }
                for bm, mod, release in manifest_rows
            ]
        manifest_payload = {
            "schema_version": 2,
            "share_id": share_id,
            "name": build_name,
            "target_game_version": target_version,
            "mods": manifest_mods,
        }
        modpack_json_bytes = json.dumps(manifest_payload, ensure_ascii=False, indent=2).encode("utf-8")

        embed = discord.Embed(
            title=f"📦 {build_name}",
            description="Сборка опубликована. Нажмите на заголовок, чтобы открыть страницу сборки.",
            url=url,
            color=discord.Color.green(),
        )
        if first_image_url and first_image_url.startswith(("http://", "https://")):
            embed.set_thumbnail(url=first_image_url)
        embed.set_author(name="Vintage Story Modpack Builder")
        embed.add_field(name="Vintage Story", value=target_version or "Не указана", inline=True)
        embed.add_field(name="Модов", value=str(len(mod_names)), inline=True)
        embed.add_field(name="Share ID", value=f"`{share_id}`", inline=True)
        embed.add_field(name="Состав", value="Полный состав и выбранные версии находятся в приложенном `modpack.json`.", inline=False)
        embed.set_footer(text="modpack.json позволяет восстановить состав сборки")
        discord_files = [discord.File(io.BytesIO(modpack_json_bytes), filename="modpack.json")]
        msg = await channel.send(embed=embed, files=discord_files)

        async with SessionLocal() as session:
            build = (await session.execute(select(Build).where(Build.share_id == share_id))).scalar_one()
            build.published = True
            build.discord_message_id = msg.id
            await session.commit()
        return {"published": True, "url": url, "message_id": msg.id}


@app.get("/build/{share_id}", include_in_schema=False)
async def build_page(share_id: str):
    return FileResponse(Path(__file__).parent / "web" / "index.html")


async def _recover_build_from_discord(share_id: str) -> bool:
    """Restore a build from its Discord attachment if its local Build row is absent.

    `modpack.json` is authoritative for current posts. For older posts, parse the historical
    `modpack.txt` file, then match names against the local catalog. Recovery is conservative:
    if even one mod cannot be mapped, don't silently create an incomplete build.
    """
    async with SessionLocal() as session:
        existing = (await session.execute(select(Build.id).where(Build.share_id == share_id))).scalar_one_or_none()
        if existing:
            return True
    if not bot.is_ready() or not settings.discord_builds_channel_id:
        return False
    channel = bot.get_channel(settings.discord_builds_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(settings.discord_builds_channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BUILD] legacy recovery cannot access Discord builds channel: %s", exc)
            return False
    if not hasattr(channel, "history"):
        return False

    expected_suffix = f"/build/{share_id}"
    matched_message = None
    try:
        async for message in channel.history(limit=1000, oldest_first=False):
            hit = share_id in (message.content or "")
            if not hit:
                hit = any(
                    str(embed.url or "").rstrip("/").endswith(expected_suffix)
                    or share_id in str(embed.title or "")
                    or share_id in str(embed.description or "")
                    or any(share_id in str(field.value or "") for field in embed.fields)
                    for embed in message.embeds
                )
            if hit:
                matched_message = message
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BUILD] legacy recovery could not scan Discord messages: %s", exc)
        return False
    if not matched_message:
        return False

    manifest = None
    text_attachment = None
    for attachment in matched_message.attachments:
        try:
            if attachment.filename.casefold() == "modpack.json":
                manifest = json.loads((await attachment.read()).decode("utf-8-sig"))
            elif attachment.filename.casefold() == "modpack.txt":
                text_attachment = (await attachment.read()).decode("utf-8-sig", errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BUILD] could not read manifest attachment %s: %s", attachment.filename, exc)

    if not isinstance(manifest, dict) or not isinstance(manifest.get("mods"), list):
        manifest = _legacy_text_manifest(text_attachment or matched_message.content or "", share_id)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("mods"), list) or not manifest["mods"]:
        logger.warning("[BUILD] no compatible manifest attached to old Discord build share_id=%s", share_id)
        return False

    from .services.identity import canonical_identifier
    from .services.builds import _release_for_game
    target_version = manifest.get("target_game_version") or manifest.get("vintage_story_version")
    name = str(manifest.get("name") or "Vintage Story Modpack")[:160]
    async with SessionLocal() as session:
        # Include hidden legacy dependency rows only for exact recovery of historical manifests.
        # They remain excluded from normal catalog/search and can never be selected in new builds.
        catalog = (await session.execute(
            select(Mod).options(selectinload(Mod.releases)).order_by(Mod.catalog_visible.desc(), Mod.id)
        )).scalars().all()
        source_rows = (await session.execute(select(DiscordSource))).scalars().all()
        title_to_mod: dict[str, int] = {}
        for source in source_rows:
            if source.mod_id and source.post_title:
                title_to_mod.setdefault(canonical_identifier(source.post_title), source.mod_id)
        resolved = []
        unresolved = []
        for entry in manifest["mods"]:
            if isinstance(entry, str):
                entry = {"name": entry}
            if not isinstance(entry, dict):
                unresolved.append(str(entry)); continue
            target = None
            url = str(entry.get("mod_db_url") or "").rstrip("/").casefold()
            asset = entry.get("mod_db_asset_id")
            mod_identifier = str(entry.get("mod_id") or "").casefold()
            canon_id = canonical_identifier(entry.get("canonical_id") or "")
            name_key = canonical_identifier(entry.get("name") or "")
            # Exact page/asset first, then real modinfo identifier, explicit Discord title, name.
            target = next((m for m in catalog if url and str(m.mod_db_url or "").rstrip("/").casefold() == url), None)
            if target is None and asset is not None:
                target = next((m for m in catalog if m.mod_db_asset_id == asset), None)
            if target is None and mod_identifier:
                target = next((m for m in catalog if str(m.mod_id or "").casefold() == mod_identifier), None)
            alias_key = canon_id or name_key
            if target is None and alias_key:
                alias_id = title_to_mod.get(alias_key)
                target = next((m for m in catalog if m.id == alias_id), None) if alias_id else None
                if target is None:
                    target = next((m for m in catalog if canonical_identifier(m.name) == alias_key), None)
            if target is None:
                unresolved.append(str(entry.get("name") or entry.get("mod_id") or "unknown mod"))
                continue
            release = next((r for r in target.releases if entry.get("selected_version") and r.mod_version == entry.get("selected_version")), None)
            if release is None:
                release = _release_for_game(target.releases, target_version)
            resolved.append((target, release, entry))

        if unresolved:
            logger.warning("[BUILD] legacy recovery refused partial build %s; unmatched mods: %s", share_id, unresolved[:20])
            return False
        # If a row with this share ID appeared while reading the manifest, leave it intact.
        build = (await session.execute(select(Build).where(Build.share_id == share_id))).scalar_one_or_none()
        if build:
            return True
        build = Build(
            share_id=share_id,
            name=name,
            target_game_version=target_version,
            published=True,
            discord_message_id=matched_message.id,
            schema_version=1,
        )
        session.add(build)
        await session.flush()
        for pos, (mod, release, entry) in enumerate(resolved):
            session.add(BuildMod(
                build_id=build.id,
                mod_id=mod.id,
                release_id=release.id if release else None,
                selected_version=(entry.get("selected_version") or (release.mod_version if release else None)),
                selected_file_url=(entry.get("selected_file_url") or entry.get("file_url")),
                selected_file_name=(entry.get("filename") or entry.get("selected_file_name")),
                auto_added=False,
                position=pos,
            ))
        await session.commit()
    logger.info("[BUILD] restored old share ID from Discord: %s (%s mods)", share_id, len(resolved))
    return True


def _legacy_text_manifest(text: str, share_id: str) -> dict:
    import re
    name = "Vintage Story Modpack"
    target_version = None
    mods = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("название:") or line.lower().startswith("name:"):
            name = line.split(":", 1)[1].strip() or name
        elif line.lower().startswith("vintage story:"):
            target_version = line.split(":", 1)[1].strip() or None
        elif re.match(r"^\d+[.)]\s+", line):
            mods.append({"name": re.sub(r"^\d+[.)]\s+", "", line).strip()})
    return {"schema_version": 0, "share_id": share_id, "name": name, "target_game_version": target_version, "mods": mods}


@app.get("/media/images/{mod_id}", include_in_schema=False)
async def media_image(mod_id: int):
    async with SessionLocal() as session:
        mod = await session.get(Mod, mod_id)
        if not mod or not mod.image_cache_path or not Path(mod.image_cache_path).exists():
            raise HTTPException(404)
        return FileResponse(mod.image_cache_path)


@app.post("/api/builds/{share_id}/prepare")
async def prepare_build(share_id: str):
    async with SessionLocal() as session:
        exists = (await session.execute(select(Build.id).where(Build.share_id == share_id))).scalar_one_or_none()
    if not exists:
        await _recover_build_from_discord(share_id)
    async def worker(job_id, jm):
        async def progress(current, total, message):
            await jm.update(job_id, current=current, total=total, message=message)

        await builds.prepare(share_id, progress)
        await jm.update(
            job_id,
            current=1,
            total=1,
            status="done",
            message="Файлы готовы. ZIP будет создан непосредственно при скачивании.",
        )

    job_id = await jobs.create("build_prepare", worker)
    return {"job_id": job_id}


@app.get("/api/builds/{share_id}/download")
async def download_build(share_id: str, background_tasks: BackgroundTasks):
    async with SessionLocal() as session:
        exists = (await session.execute(select(Build.id).where(Build.share_id == share_id))).scalar_one_or_none()
    if not exists:
        await _recover_build_from_discord(share_id)
    temp_path, filename = await builds.build_temp_archive(share_id)
    background_tasks.add_task(_delete_temp_file, temp_path)
    return FileResponse(temp_path, media_type="application/zip", filename=filename, background=background_tasks)


def _delete_temp_file(path: Path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def serialize_mod(m: Mod):
    # Dependency IDs are sent with the catalog so the browser can expand a selection locally
    # without making an extra request for every clicked card.
    # This endpoint is called in the context of a loaded async session, so the relation query is
    # intentionally performed in the route helper below instead of lazy-loading here.
    return {
        "id": m.id,
        "name": m.name,
        "mod_db_id": m.mod_db_id,
        "mod_db_asset_id": m.mod_db_asset_id,
        "mod_id": m.mod_id,
        "display_mod_id": m.mod_id or (str(m.mod_db_id) if m.mod_db_id is not None else (str(m.mod_db_asset_id) if m.mod_db_asset_id is not None else None)),
        "description": m.short_description or m.description,
        "short_description": m.short_description,
        "image_url": f"/media/images/{m.id}" if m.image_cache_path else m.image_url,
        "author": m.author,
        "mod_type": m.mod_type,
        "mod_db_url": m.mod_db_url,
        "supported_versions": json.loads(m.supported_versions_json or "[]"),
        "latest_game_version": m.latest_game_version,
        "latest_file_name": m.latest_file_name,
        "parse_status": m.parse_status,
        "parse_message": m.parse_message,
        "refreshed_at": m.refreshed_at.isoformat() if m.refreshed_at else None,
        "parent_mod_id": m.parent_mod_id,
        "origin": m.origin,
    }


def serialize_relation(r, targets):
    target = targets.get(r.target_mod_id) if r.target_mod_id else None
    return {
        "relation_type": r.relation_type,
        "target_mod_id": r.target_mod_id,
        "target_name": target.name if target else r.target_name,
        "target_url": target.mod_db_url if target else getattr(r, "target_url", None),
        "confidence": r.confidence,
        "verified": r.verified,
        "evidence": r.evidence,
        "raw_phrase": r.raw_phrase,
        "source_kind": getattr(r, "source_kind", "html"),
        "required_version": getattr(r, "required_version", None),
    }
