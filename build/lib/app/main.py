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

from .config import settings
from .db import SessionLocal, init_db
from .models import Addon, Build, BuildMod, Compatibility, Dependency, Mod, ModRelease
from .services.builds import BuildService
from .services.cache import CacheManager
from .services.discord_ingest import DiscordCatalogScanner
from .services.jobs import JobManager
from .services.moddb import ModDBClient
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
builds = BuildService(cache, moddb)


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
    if settings.discord_token:
        discord_task = asyncio.create_task(_start_discord())


@app.on_event("shutdown")
async def shutdown():
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


@app.get("/api/catalog")
async def catalog():
    async with SessionLocal() as session:
        mods = (await session.execute(select(Mod).order_by(Mod.name))).scalars().all()
        return [serialize_mod(m) for m in mods]


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
                    "target_name": a.title,
                    "target_url": a.url,
                    "confidence": 1.0,
                    "verified": True,
                    "evidence": "Discord thread",
                    "raw_phrase": "addon",
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
    service = CatalogSyncService(scanner, moddb, cache)
    job_id = await jobs.create("catalog_sync", service.run)
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
        "url": f"{settings.public_base_url.rstrip('/')}/build/{build.share_id}",
        "published": False,
    }


@app.get("/api/builds/{share_id}")
async def get_build(share_id: str):
    async with SessionLocal() as session:
        build = (
            await session.execute(select(Build).where(Build.share_id == share_id))
        ).scalar_one_or_none()
        if not build:
            raise HTTPException(404)
        items = (
            await session.execute(
                select(BuildMod, Mod, ModRelease)
                .join(Mod, Mod.id == BuildMod.mod_id)
                .outerjoin(ModRelease, ModRelease.id == BuildMod.release_id)
                .where(BuildMod.build_id == build.id)
                .order_by(BuildMod.position)
            )
        ).all()
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

        modpack_lines = [
            f"Название: {build_name}",
            f"Vintage Story: {target_version or 'не указана'}",
            f"Share ID: {share_id}",
            f"Ссылка: {url}",
            "",
            "Моды:",
        ]
        modpack_lines.extend(f"{idx}. {name}" for idx, name in enumerate(mod_names, 1))
        modpack_bytes = "\n".join(modpack_lines).encode("utf-8")

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
        embed.add_field(name="Состав", value="Полный список прикреплён отдельным файлом `modpack.txt`.", inline=False)
        embed.set_footer(text="modpack.txt содержит полный список модов; ссылка ведёт на страницу сборки")
        discord_file = discord.File(io.BytesIO(modpack_bytes), filename="modpack.txt")
        msg = await channel.send(embed=embed, file=discord_file)

        async with SessionLocal() as session:
            build = (await session.execute(select(Build).where(Build.share_id == share_id))).scalar_one()
            build.published = True
            build.discord_message_id = msg.id
            await session.commit()
        return {"published": True, "url": url, "message_id": msg.id}


@app.get("/build/{share_id}", include_in_schema=False)
async def build_page(share_id: str):
    return FileResponse(Path(__file__).parent / "web" / "index.html")


@app.get("/media/images/{mod_id}", include_in_schema=False)
async def media_image(mod_id: int):
    async with SessionLocal() as session:
        mod = await session.get(Mod, mod_id)
        if not mod or not mod.image_cache_path or not Path(mod.image_cache_path).exists():
            raise HTTPException(404)
        return FileResponse(mod.image_cache_path)


@app.post("/api/builds/{share_id}/prepare")
async def prepare_build(share_id: str):
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
    temp_path, filename = await builds.build_temp_archive(share_id)
    background_tasks.add_task(_delete_temp_file, temp_path)
    return FileResponse(temp_path, media_type="application/zip", filename=filename, background=background_tasks)


def _delete_temp_file(path: Path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def serialize_mod(m: Mod):
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
    }
