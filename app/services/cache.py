from pathlib import Path
import asyncio
import hashlib
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

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

    async def cached_path(self, url: str, filename: str) -> Path | None:
        cache_key = self.key(url)
        async with SessionLocal() as session:
            existing = (
                await session.execute(select(CachedFile).where(CachedFile.cache_key == cache_key))
            ).scalar_one_or_none()
        if existing:
            path = Path(existing.path)
            if path.exists():
                return path
        candidate = self.file_dir / f"{cache_key[:16]}-{Path(filename).name}"
        return candidate if candidate.exists() else None

    async def fetch_file(self, url: str, filename: str) -> Path:
        existing = await self.cached_path(url, filename)
        if existing:
            return existing
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
            existing = (
                await session.execute(select(CachedFile).where(CachedFile.cache_key == self.key(url)))
            ).scalar_one_or_none()
            if existing:
                existing.path = str(path)
                existing.filename = safe_name
                existing.size_bytes = path.stat().st_size
                existing.sha256 = sha
            else:
                session.add(
                    CachedFile(
                        cache_key=self.key(url),
                        url=url,
                        path=str(path),
                        filename=safe_name,
                        size_bytes=path.stat().st_size,
                        sha256=sha,
                    )
                )
            await session.commit()
        return path

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
