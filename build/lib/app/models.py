from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Mod(Base):
    __tablename__ = "mods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mod_db_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # Public Mod DB page/asset ID used by /show/mod/{assetid}. Different namespace from modid.
    mod_db_asset_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # Vintage Story modinfo identifier.
    mod_id: Mapped[str | None] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    short_description: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    image_cache_path: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(300))
    mod_type: Mapped[str | None] = mapped_column(String(80))
    mod_db_url: Mapped[str | None] = mapped_column(Text)
    supported_versions_json: Mapped[str] = mapped_column(Text, default="[]")
    latest_game_version: Mapped[str | None] = mapped_column(String(80))
    latest_release_id: Mapped[int | None] = mapped_column(Integer)
    latest_file_url: Mapped[str | None] = mapped_column(Text)
    latest_file_name: Mapped[str | None] = mapped_column(String(500))
    source_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    parse_status: Mapped[str] = mapped_column(String(50), default="ok")
    parse_message: Mapped[str | None] = mapped_column(Text)

    releases: Mapped[list["ModRelease"]] = relationship(back_populates="mod", cascade="all, delete-orphan")
    sources: Mapped[list["DiscordSource"]] = relationship(back_populates="mod", cascade="all, delete-orphan")
    addons: Mapped[list["Addon"]] = relationship(back_populates="mod", cascade="all, delete-orphan")


class ModRelease(Base):
    __tablename__ = "mod_releases"
    __table_args__ = (UniqueConstraint("mod_id", "release_id", name="uq_mod_release"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mod_id: Mapped[int] = mapped_column(ForeignKey("mods.id", ondelete="CASCADE"), index=True)
    release_id: Mapped[int] = mapped_column(Integer, index=True)
    mod_version: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str | None] = mapped_column(String(500))
    file_id: Mapped[int | None] = mapped_column(Integer)
    file_url: Mapped[str | None] = mapped_column(Text)
    game_versions_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changelog: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)
    mod: Mapped["Mod"] = relationship(back_populates="releases")


class Addon(Base):
    __tablename__ = "addons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mod_id: Mapped[int] = mapped_column(ForeignKey("mods.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text)
    discord_message_id: Mapped[int | None] = mapped_column(Integer)
    discord_thread_id: Mapped[int | None] = mapped_column(Integer)
    source_hash: Mapped[str | None] = mapped_column(String(128))
    mod: Mapped["Mod"] = relationship(back_populates="addons")


class Dependency(Base):
    __tablename__ = "dependencies"
    __table_args__ = (UniqueConstraint("mod_id", "target_mod_id", "relation_type", name="uq_dependency"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mod_id: Mapped[int] = mapped_column(ForeignKey("mods.id", ondelete="CASCADE"), index=True)
    target_mod_id: Mapped[int | None] = mapped_column(ForeignKey("mods.id", ondelete="SET NULL"))
    target_name: Mapped[str | None] = mapped_column(String(300))
    target_url: Mapped[str | None] = mapped_column(Text)
    relation_type: Mapped[str] = mapped_column(String(40))
    raw_phrase: Mapped[str | None] = mapped_column(String(200))
    evidence: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(default=0.5)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class Compatibility(Base):
    __tablename__ = "compatibilities"
    __table_args__ = (UniqueConstraint("mod_id", "target_mod_id", "relation_type", name="uq_compatibility"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mod_id: Mapped[int] = mapped_column(ForeignKey("mods.id", ondelete="CASCADE"), index=True)
    target_mod_id: Mapped[int | None] = mapped_column(ForeignKey("mods.id", ondelete="SET NULL"))
    target_name: Mapped[str | None] = mapped_column(String(300))
    target_url: Mapped[str | None] = mapped_column(Text)
    relation_type: Mapped[str] = mapped_column(String(40))
    raw_phrase: Mapped[str | None] = mapped_column(String(200))
    evidence: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(default=0.5)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class DiscordSource(Base):
    __tablename__ = "discord_sources"
    __table_args__ = (UniqueConstraint("thread_id", "message_id", name="uq_discord_message"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mod_id: Mapped[int | None] = mapped_column(ForeignKey("mods.id", ondelete="CASCADE"), index=True)
    guild_id: Mapped[int] = mapped_column(Integer, index=True)
    category_id: Mapped[int | None] = mapped_column(Integer)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)
    thread_id: Mapped[int] = mapped_column(Integer, index=True)
    message_id: Mapped[int] = mapped_column(Integer, index=True)
    post_title: Mapped[str] = mapped_column(String(300))
    message_content: Mapped[str] = mapped_column(Text)
    mod_db_url: Mapped[str | None] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(128), index=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    mod: Mapped["Mod | None"] = relationship(back_populates="sources")


class Build(Base):
    __tablename__ = "builds"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    share_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), default="Vintage Story Modpack")
    target_game_version: Mapped[str | None] = mapped_column(String(80))
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    discord_message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    mods: Mapped[list["BuildMod"]] = relationship(
        back_populates="build", cascade="all, delete-orphan", order_by="BuildMod.position"
    )


class BuildMod(Base):
    __tablename__ = "build_mods"
    __table_args__ = (UniqueConstraint("build_id", "mod_id", name="uq_build_mod"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id", ondelete="CASCADE"), index=True)
    mod_id: Mapped[int] = mapped_column(ForeignKey("mods.id", ondelete="RESTRICT"))
    release_id: Mapped[int | None] = mapped_column(ForeignKey("mod_releases.id", ondelete="SET NULL"))
    selected_version: Mapped[str | None] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer)
    build: Mapped["Build"] = relationship(back_populates="mods")


class CachedFile(Base):
    __tablename__ = "cached_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    url: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    current: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
