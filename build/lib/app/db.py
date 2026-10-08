from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.db_url,
    future=True,
    pool_pre_ping=True,
    connect_args={"timeout": settings.sqlite_busy_timeout_seconds},
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_seconds * 1000)}")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def init_db() -> None:
    from .models import (  # noqa: F401
        Addon,
        Build,
        BuildMod,
        CachedFile,
        Compatibility,
        Dependency,
        DiscordSource,
        Job,
        Mod,
        ModRelease,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_schema)


def _migrate_schema(sync_conn) -> None:
    columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(mods)").fetchall()}
    if "short_description" not in columns:
        sync_conn.exec_driver_sql("ALTER TABLE mods ADD COLUMN short_description TEXT")
    if "mod_db_asset_id" not in columns:
        sync_conn.exec_driver_sql("ALTER TABLE mods ADD COLUMN mod_db_asset_id INTEGER")
    dep_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(dependencies)").fetchall()}
    if "target_url" not in dep_columns:
        sync_conn.exec_driver_sql("ALTER TABLE dependencies ADD COLUMN target_url TEXT")
    comp_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(compatibilities)").fetchall()}
    if "target_url" not in comp_columns:
        sync_conn.exec_driver_sql("ALTER TABLE compatibilities ADD COLUMN target_url TEXT")

    build_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(builds)").fetchall()}
    if "name" not in build_columns:
        sync_conn.exec_driver_sql(
            "ALTER TABLE builds ADD COLUMN name VARCHAR(160) NOT NULL DEFAULT 'Vintage Story Modpack'"
        )


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
