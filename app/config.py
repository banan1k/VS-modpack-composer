from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "VS Modpack Builder"
    host: str = "127.0.0.1"
    port: int = 8000
    public_base_url: str = "http://localhost:8000"
    db_url: str = "sqlite+aiosqlite:///./data/app.db"
    data_dir: Path = Path("./data")
    moddb_base_url: str = "https://mods.vintagestory.at"
    moddb_api_base_url: str = "https://mods.vintagestory.at/api"
    http_timeout_seconds: float = 30.0
    http_concurrency: int = 8
    download_concurrency: int = 4
    sqlite_busy_timeout_seconds: float = 30.0
    relation_min_confidence: float = 0.90
    catalog_source_version: str = "9"

    discord_token: str | None = None
    discord_guild_id: int | None = None
    # Comma/semicolon-separated list of Forum Channel IDs to scan.
    # Example: DISCORD_FORUM_CHANNEL_IDS=123456789012345678,234567890123456789
    discord_forum_channel_ids: str = ""
    discord_builds_channel_id: int | None = None
    discord_registry_channel_id: int | None = None

    @property
    def forum_channel_ids(self) -> list[int]:
        values = self.discord_forum_channel_ids.replace(";", ",").split(",")
        ids: list[int] = []
        for value in values:
            value = value.strip()
            if not value:
                continue
            try:
                ids.append(int(value))
            except ValueError:
                raise ValueError(f"Invalid Discord forum channel ID: {value!r}") from None
        return list(dict.fromkeys(ids))

    @property
    def image_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "images"

    @property
    def file_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "files"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_cache_dir.mkdir(parents=True, exist_ok=True)
        self.file_cache_dir.mkdir(parents=True, exist_ok=True)
        # Remove archives produced by older versions. New versions never persist compiled builds.
        legacy_archive_dir = self.data_dir / "cache" / "archives"
        if legacy_archive_dir.exists():
            for archive in legacy_archive_dir.glob("*.zip"):
                archive.unlink(missing_ok=True)
            try:
                legacy_archive_dir.rmdir()
            except OSError:
                pass
        # Remove abandoned temporary build archives left by interrupted downloads.
        import tempfile
        import time
        cutoff = time.time() - 24 * 60 * 60
        for tmp in Path(tempfile.gettempdir()).glob("vs-modpack-*.zip"):
            try:
                if tmp.stat().st_mtime < cutoff:
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass


settings = Settings()
