from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, select

from ..db import SessionLocal
from ..models import CachedFile, Mod


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

    @staticmethod
    def _safe_filename(filename: str, url: str | None = None) -> str:
        # Windows paths may contain backslashes even when this service runs on another OS.
        raw = str(filename or "").replace("\\", "/").split("/")[-1].strip()
        if not raw and url:
            raw = urlparse(url).path.rsplit("/", 1)[-1]
        # Mod DB and old cache versions sometimes prefix filenames with a 16-hex content/hash ID.
        # The cache is stored in per-mod directories now, so this prefix is redundant and confusing.
        raw = re.sub(r"^[0-9a-fA-F]{16}-", "", raw)
        raw = raw.replace(":", "_").replace("\x00", "").strip(" .")
        return raw or "mod.zip"

    @staticmethod
    def _folder_component(name: str | None, mod_identifier: str | None, owner_mod_id: int | None) -> str:
        # Friendly but stable folder names, e.g. `Fixed-Paths [fixedpaths]`; append modinfo ID
        # so forks with the same display name do not collide.
        name = " ".join(str(name or "").split()).strip()
        identifier = str(mod_identifier or "").strip()
        if name and identifier and name.casefold() != identifier.casefold():
            raw = f"{name} [{identifier}]"
        else:
            raw = name or identifier or (f"mod-{owner_mod_id}" if owner_mod_id is not None else "shared")
        raw = re.sub(r"[^\w .()\[\]-]+", "", raw, flags=re.UNICODE)
        raw = re.sub(r"\s+", "-", raw).strip(" .-")
        return raw[:100] or (f"mod-{owner_mod_id}" if owner_mod_id is not None else "shared")

    def _target_path(self, owner_mod_id: int | None, filename: str, owner_folder: str | None = None) -> Path:
        # Per-mod named folders prevent collisions while keeping ZIP filenames readable.
        owner = owner_folder or (f"mod-{owner_mod_id}" if owner_mod_id is not None else "shared")
        return self.file_dir / owner / self._safe_filename(filename)

    async def migrate_cache_layout(self) -> None:
        """Move legacy numeric-ID cache folders into human-readable per-mod folders."""
        async with SessionLocal() as session:
            rows = (await session.execute(select(CachedFile).order_by(CachedFile.id))).scalars().all()
            mods = (await session.execute(select(Mod.id, Mod.name, Mod.mod_id))).all()
            owner_folders = {
                int(mod_id): self._folder_component(name, identifier, int(mod_id))
                for mod_id, name, identifier in mods
            }
            for row in rows:
                source = Path(row.path)
                clean_name = self._safe_filename(row.filename, row.url)
                owner_folder = owner_folders.get(row.owner_mod_id, "shared")
                target = self._target_path(row.owner_mod_id, clean_name, owner_folder)
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.exists() and source.resolve() != target.resolve():
                    if target.exists():
                        try:
                            same_content = (
                                source.stat().st_size == target.stat().st_size
                                and bool(row.sha256)
                                and hashlib.sha256(target.read_bytes()).hexdigest() == row.sha256
                            )
                        except OSError:
                            same_content = False
                        if same_content:
                            source.unlink(missing_ok=True)
                        else:
                            # Duplicate filenames can occur across historical rows of one mod.
                            n = 2
                            stem, suffix = Path(clean_name).stem, Path(clean_name).suffix
                            while target.exists():
                                target = self._target_path(
                                    row.owner_mod_id, f"{stem} ({n}){suffix}", owner_folder
                                )
                                n += 1
                            source.replace(target)
                            clean_name = target.name
                    else:
                        source.replace(target)
                elif not source.exists() and target.exists():
                    # Never let a stale database row point at an unrelated file merely because
                    # it has the same archive name. If the old bytes are gone and the checksum
                    # doesn't prove this target is identical, drop only the stale metadata row;
                    # the correct file (if any) stays untouched and the next request can refetch.
                    same_content = False
                    if row.sha256:
                        try:
                            same_content = hashlib.sha256(target.read_bytes()).hexdigest() == row.sha256
                        except OSError:
                            same_content = False
                    if not same_content:
                        await session.delete(row)
                        continue
                # If a file is absent at both paths, keep its canonical destination in metadata;
                # cached_path() will miss it and fetch_file() will recreate it on demand.
                row.path = str(target)
                row.filename = target.name if source.exists() or target.exists() else clean_name
            await session.commit()

        # Remove emptied numeric/legacy directories after all paths have been migrated.
        if self.file_dir.exists():
            directories = sorted(
                (p for p in self.file_dir.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts), reverse=True,
            )
            for directory in directories:
                try:
                    next(directory.iterdir())
                except StopIteration:
                    directory.rmdir()
                except OSError:
                    pass

    async def _get_owner_folder(self, owner_mod_id: int | None) -> str:
        if owner_mod_id is None:
            return "shared"
        async with SessionLocal() as session:
            mod = await session.get(Mod, owner_mod_id)
            if mod is None:
                return f"mod-{owner_mod_id}"
            return self._folder_component(mod.name, mod.mod_id, owner_mod_id)

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
                    owner_folder = await self._get_owner_folder(owner_mod_id)
                    relocated = await self._touch_owner(
                        cache_key, owner_mod_id, owner_release_id, game_version, owner_folder
                    )
                    return relocated or existing
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
            safe_name = self._safe_filename(filename or Path(urlparse(url).path).name or "mod.zip", url)
            owner_folder = await self._get_owner_folder(owner_mod_id)
            path = self._target_path(owner_mod_id, safe_name, owner_folder)
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

    async def _touch_owner(
        self,
        cache_key: str,
        owner_mod_id: int,
        owner_release_id: int | None,
        game_version: str | None,
        owner_folder: str | None = None,
    ) -> Path | None:
        async with SessionLocal() as session:
            row = (await session.execute(
                select(CachedFile).where(CachedFile.cache_key == cache_key)
            )).scalar_one_or_none()
            if row is None:
                return None
            source = Path(row.path)
            target = self._target_path(owner_mod_id, row.filename, owner_folder)
            if source.exists() and source.resolve() != target.resolve():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if source.stat().st_size == target.stat().st_size and row.sha256 and hashlib.sha256(target.read_bytes()).hexdigest() == row.sha256:
                        source.unlink(missing_ok=True)
                    else:
                        n = 2
                        stem, suffix = target.stem, target.suffix
                        while target.exists():
                            target = self._target_path(owner_mod_id, f"{stem} ({n}){suffix}", owner_folder)
                            n += 1
                        source.replace(target)
                else:
                    source.replace(target)
            row.path = str(target)
            row.filename = target.name
            row.owner_mod_id = owner_mod_id
            row.owner_release_id = owner_release_id
            row.game_version = game_version
            await session.commit()
            return target

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
        safe_name = self._safe_filename(filename_hint or "image", url)
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
