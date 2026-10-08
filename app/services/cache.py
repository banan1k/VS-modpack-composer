from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, select

from ..db import SessionLocal
from ..models import CachedFile


class CacheManager:
    def __init__(self, file_dir: Path, image_dir: Path, timeout: float = 30.0, download_concurrency: int = 4):
        self.file_dir = file_dir
        self.image_dir = image_dir
        self.timeout = timeout
        self.download_concurrency = download_concurrency
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._file_locks: dict[str, asyncio.Lock] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def _lock_for(self, url: str) -> asyncio.Lock:
        key = self.key(url)
        lock = self._file_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._file_locks[key] = lock
        return lock

    async def cached_path(
        self,
        url: str,
        filename: str,
        *,
        expected_release_id: int | None = None,
    ) -> Path | None:
        cache_key = self.key(url)
        async with SessionLocal() as session:
            existing = (
                await session.execute(
                    select(CachedFile).where(CachedFile.cache_key == cache_key)
                )
            ).scalar_one_or_none()
        if existing:
            # A background prefetch knows exactly which Mod DB release it requested. If the
            # cache still points at another release under the same URL, treat it as stale. We do
            # not delete it here because a caller may only be checking; fetch_file(...,
            # replace_stale=True) performs the replacement atomically under the URL lock.
            if expected_release_id is not None and existing.owner_release_id not in (None, expected_release_id):
                return None
            path = Path(existing.path)
            if path.exists():
                return path
            return None
        # Do not resurrect an orphaned file that has no CachedFile row: version control relies on
        # the metadata row, so the next fetch should rebuild it cleanly.
        return None

    async def fetch_file(
        self,
        url: str,
        filename: str,
        *,
        owner_mod_id: int | None = None,
        owner_release_id: int | None = None,
        game_version: str | None = None,
        expected_release_id: int | None = None,
        replace_stale: bool = False,
    ) -> Path:
        async with self._lock_for(url):
            cache_key = self.key(url)
            async with SessionLocal() as session:
                cached_row = (
                    await session.execute(
                        select(CachedFile).where(CachedFile.cache_key == cache_key)
                    )
                ).scalar_one_or_none()

            existing = await self.cached_path(
                url, filename, expected_release_id=expected_release_id
            )
            if existing:
                if owner_mod_id:
                    await self._touch_owner(cache_key, owner_mod_id, owner_release_id, game_version)
                return existing

            # If prefetch discovered that the cached bytes belong to an older release but the
            # URL stayed the same, explicitly remove that stale cache record/file before writing
            # the new bytes. Normal user build downloads never set replace_stale=True, so they do
            # not accidentally evict the background cache.
            if replace_stale and cached_row is not None:
                stale_path = Path(cached_row.path)
                try:
                    stale_path.unlink(missing_ok=True)
                except OSError:
                    pass
                async with SessionLocal() as session:
                    row = (
                        await session.execute(
                            select(CachedFile).where(CachedFile.cache_key == cache_key)
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        await session.delete(row)
                        await session.commit()
            safe_name = Path(filename or Path(urlparse(url).path).name or "mod.zip").name
            path = self.file_dir / f"{self.key(url)[:16]}-{safe_name}"
            path.parent.mkdir(parents=True, exist_ok=True)
            client = await self._get_client()
            tmp = path.with_suffix(path.suffix + ".part")
            try:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with tmp.open("wb") as f:
                        async for chunk in response.aiter_bytes(1024 * 64):
                            f.write(chunk)
                tmp.replace(path)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            async with SessionLocal() as session:
                cache_key = self.key(url)
                existing = (await session.execute(select(CachedFile).where(CachedFile.cache_key == cache_key))).scalar_one_or_none()
                if existing:
                    existing.path = str(path)
                    existing.filename = safe_name
                    existing.size_bytes = path.stat().st_size
                    existing.sha256 = sha
                    existing.owner_mod_id = owner_mod_id or existing.owner_mod_id
                    existing.owner_release_id = owner_release_id or existing.owner_release_id
                    existing.game_version = game_version or existing.game_version
                else:
                    session.add(CachedFile(
                        cache_key=cache_key,
                        url=url,
                        path=str(path),
                        filename=safe_name,
                        owner_mod_id=owner_mod_id,
                        owner_release_id=owner_release_id,
                        game_version=game_version,
                        size_bytes=path.stat().st_size,
                        sha256=sha,
                    ))
                await session.commit()
            return path

    async def _touch_owner(self, cache_key: str, owner_mod_id: int, owner_release_id: int | None, game_version: str | None):
        async with SessionLocal() as session:
            row = (await session.execute(select(CachedFile).where(CachedFile.cache_key == cache_key))).scalar_one_or_none()
            if row:
                row.owner_mod_id = owner_mod_id
                row.owner_release_id = owner_release_id
                row.game_version = game_version
                await session.commit()

    async def purge_mod_files(self, owner_mod_id: int):
        """Remove every cached archive associated with a mod."""
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(CachedFile).where(CachedFile.owner_mod_id == owner_mod_id)
                )
            ).scalars().all()
            for row in rows:
                try:
                    Path(row.path).unlink(missing_ok=True)
                except OSError:
                    pass
                await session.delete(row)
            if rows:
                await session.commit()

    async def purge_obsolete_mod_files(self, owner_mod_id: int, keep_url: str):
        keep_key = self.key(keep_url)
        async with SessionLocal() as session:
            rows = (await session.execute(select(CachedFile).where(CachedFile.owner_mod_id == owner_mod_id))).scalars().all()
            remove = [r for r in rows if r.cache_key != keep_key]
            for row in remove:
                path = Path(row.path)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                await session.delete(row)
            if remove:
                await session.commit()

    async def fetch_image(self, url: str, filename_hint: str = "image") -> Path:
        cache_key = self.key(url)
        safe_name = Path(filename_hint or "image").name
        suffix = Path(urlparse(url).path).suffix or Path(safe_name).suffix or ".img"
        path = self.image_dir / f"{cache_key[:24]}{suffix}"
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        client = await self._get_client()
        response = await client.get(url)
        response.raise_for_status()
        path.write_bytes(response.content)
        return path
