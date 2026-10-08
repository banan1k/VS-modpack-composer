from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import discord

from ..config import settings

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>)\]]+", re.I)


@dataclass
class DiscordModRecord:
    title: str
    mod_db_url: str
    guild_id: int
    category_id: int | None
    channel_id: int
    thread_id: int
    starter_message_id: int
    starter_content: str
    source_hash: str
    addons: list[dict[str, Any]]


@dataclass
class DiscordScanResult:
    records: list[DiscordModRecord]
    total_threads: int
    scanned_threads: int
    skipped_no_mod_link: int
    errors: list[str]


class DiscordCatalogScanner:
    def __init__(self, client: discord.Client):
        self.client = client

    async def scan(self, progress=None) -> DiscordScanResult:
        if not settings.discord_guild_id:
            raise RuntimeError("DISCORD_GUILD_ID is not configured")
        if not settings.forum_channel_ids:
            raise RuntimeError("DISCORD_FORUM_CHANNEL_IDS is empty. Add one or more Discord Forum Channel IDs.")
        if not self.client.is_ready():
            raise RuntimeError("Discord bot is not connected yet. Make sure the bot is online and try again.")

        guild = self.client.get_guild(settings.discord_guild_id)
        if guild is None:
            guild = await self.client.fetch_guild(settings.discord_guild_id)

        forums: list[discord.ForumChannel] = []
        seen_channels: set[int] = set()
        for channel_id in settings.forum_channel_ids:
            if channel_id in seen_channels:
                continue
            seen_channels.add(channel_id)
            channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
            if not isinstance(channel, discord.ForumChannel):
                raise RuntimeError(
                    f"Discord channel {channel_id} is not a Forum Channel (actual type: {type(channel).__name__})."
                )
            if channel.guild.id != guild.id:
                raise RuntimeError(
                    f"Discord Forum Channel {channel_id} belongs to another guild ({channel.guild.id})."
                )
            forums.append(channel)

        threads: list[discord.Thread] = []
        for forum in forums:
            collected = await self._collect_threads(forum)
            threads.extend(collected)
            logger.info("[DISCORD] forum #%s: %s threads", forum.name, len(collected))

        total = len(threads)
        if progress:
            await progress(0, total, f"Discord: 0/{total}, найдено модов: 0")

        sem = asyncio.Semaphore(max(1, settings.http_concurrency))
        counter = 0
        found = 0
        skipped = 0
        errors: list[str] = []
        lock = asyncio.Lock()

        async def scan_one(thread: discord.Thread):
            nonlocal counter, found, skipped
            async with sem:
                try:
                    result = await self._scan_thread(thread.parent if isinstance(thread.parent, discord.ForumChannel) else None, thread)
                    error = None
                except Exception as exc:  # noqa: BLE001
                    result = None
                    error = f"thread {thread.id} ({thread.name}): {type(exc).__name__}: {exc}"
                    logger.exception("[DISCORD] thread scan failed: %s", error)

            async with lock:
                counter += 1
                if result:
                    found += 1
                else:
                    skipped += 1
                if error:
                    errors.append(error)
                current = counter
                found_count = found
                skipped_count = skipped

            if progress:
                suffix = f", найдено модов: {found_count}, пропущено: {skipped_count}, ошибок: {len(errors)}"
                await progress(current, total, f"Discord: {current}/{total}{suffix}")
            return result

        results = await asyncio.gather(*(scan_one(thread) for thread in threads), return_exceptions=False)
        records = [r for r in results if r is not None]
        complete = not errors
        logger.info(
            "[DISCORD] scan finished: threads=%s records=%s skipped_no_link=%s errors=%s complete=%s",
            total,
            len(records),
            skipped,
            len(errors),
            complete,
        )
        return DiscordScanResult(records, total, counter, skipped, errors)

    async def _collect_threads(self, forum: discord.ForumChannel) -> list[discord.Thread]:
        threads: list[discord.Thread] = list(forum.threads)
        existing_ids = {t.id for t in threads}
        async for thread in forum.archived_threads(limit=None):
            if thread.id not in existing_ids:
                threads.append(thread)
                existing_ids.add(thread.id)
        return threads

    async def _scan_thread(self, forum: discord.ForumChannel | None, thread: discord.Thread) -> DiscordModRecord | None:
        try:
            starter = await thread.fetch_message(thread.id)
        except discord.HTTPException as exc:
            raise RuntimeError(f"starter message fetch failed: {exc}") from exc

        starter_content, mod_db_url = _extract_moddb_url_from_message(starter)
        if not mod_db_url:
            logger.warning("[DISCORD] no Mod DB URL: thread=%s title=%r", thread.id, thread.name)
            return None

        addons: list[dict[str, Any]] = []
        try:
            async for msg in thread.history(limit=None, oldest_first=False):
                if msg.id == starter.id:
                    continue
                message_text, url = _extract_moddb_url_from_message(msg)
                title, parsed_url = self._parse_addon_message(message_text, url)
                if title and parsed_url:
                    addons.append({"title": title, "url": parsed_url, "message_id": msg.id})
        except discord.HTTPException as exc:
            logger.warning("[DISCORD] addon history failed thread=%s: %s", thread.id, exc)

        payload = {
            "source_version": settings.catalog_source_version,
            "title": thread.name,
            "content": starter_content,
            "url": mod_db_url,
            "addons": addons,
        }
        source_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return DiscordModRecord(
            title=thread.name,
            mod_db_url=mod_db_url,
            guild_id=thread.guild.id,
            category_id=thread.parent_id,
            channel_id=thread.parent_id or 0,
            thread_id=thread.id,
            starter_message_id=starter.id,
            starter_content=starter_content,
            source_hash=source_hash,
            addons=addons,
        )

    @staticmethod
    def _parse_addon_message(content: str, embedded_url: str | None = None) -> tuple[str | None, str | None]:
        url = embedded_url
        if not url:
            urls = [u for u in URL_RE.findall(content) if _is_moddb_url(u)]
            url = urls[0] if urls else None
        if not url:
            return None, None
        first = content.splitlines()[0].strip() if content.splitlines() else ""
        title = re.sub(r"https?://\S+", "", first).strip(" -–—:[]()")
        return (title or "Addon"), _clean_url(url)


def _extract_moddb_url_from_message(message: discord.Message) -> tuple[str, str | None]:
    chunks: list[str] = [message.content or ""]
    urls: list[str] = []
    for embed in message.embeds:
        if embed.url:
            urls.append(str(embed.url))
        if embed.title:
            chunks.append(str(embed.title))
        if embed.description:
            chunks.append(str(embed.description))
    text = "\n".join(chunks)
    urls.extend(URL_RE.findall(text))
    mod_url = next((_clean_url(u) for u in urls if _is_moddb_url(u)), None)
    return text, mod_url


def _is_moddb_url(url: str) -> bool:
    value = str(url).strip().strip("<>[]()")
    parsed = re.match(r"https?://(?:www\.)?mods\.vintagestory\.at/", value, re.I)
    if not parsed:
        return False
    path = value.split("mods.vintagestory.at/", 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
    if path.startswith(("api/", "download/", "show/user/", "list/", "versionchecker")):
        return False
    return bool(path) and (path.startswith("show/mod/") or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,119}", path))


def _clean_url(url: str) -> str:
    return str(url).strip().rstrip(".,!?;:").rstrip(")]}")
