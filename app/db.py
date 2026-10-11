from collections.abc import AsyncIterator
import logging

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

logger = logging.getLogger(__name__)


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
        DependencyRequest,
        DiscordSource,
        Job,
        Mod,
        ModRelease,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_schema)
    await _backfill_canonical_ids()
    await _dedupe_dependency_mods()
    async with engine.begin() as conn:
        await conn.run_sync(_create_partial_indexes)


def _migrate_schema(sync_conn) -> None:
    columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(mods)").fetchall()}
    if "short_description" not in columns:
        sync_conn.exec_driver_sql("ALTER TABLE mods ADD COLUMN short_description TEXT")
    if "mod_db_asset_id" not in columns:
        sync_conn.exec_driver_sql("ALTER TABLE mods ADD COLUMN mod_db_asset_id INTEGER")
    if "canonical_id" not in columns:
        sync_conn.exec_driver_sql("ALTER TABLE mods ADD COLUMN canonical_id VARCHAR(180)")
    if "tags_json" not in columns:
        sync_conn.exec_driver_sql("ALTER TABLE mods ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
    dep_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(dependencies)").fetchall()}
    if "target_url" not in dep_columns:
        sync_conn.exec_driver_sql("ALTER TABLE dependencies ADD COLUMN target_url TEXT")
    if "required_version" not in dep_columns:
        sync_conn.exec_driver_sql("ALTER TABLE dependencies ADD COLUMN required_version VARCHAR(120)")
    if "source_kind" not in dep_columns:
        sync_conn.exec_driver_sql("ALTER TABLE dependencies ADD COLUMN source_kind VARCHAR(30) NOT NULL DEFAULT 'html'")
    comp_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(compatibilities)").fetchall()}
    if "target_url" not in comp_columns:
        sync_conn.exec_driver_sql("ALTER TABLE compatibilities ADD COLUMN target_url TEXT")
    if "source_kind" not in comp_columns:
        sync_conn.exec_driver_sql("ALTER TABLE compatibilities ADD COLUMN source_kind VARCHAR(30) NOT NULL DEFAULT 'html'")

    addon_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(addons)").fetchall()}
    if "addon_mod_id" not in addon_columns:
        sync_conn.exec_driver_sql("ALTER TABLE addons ADD COLUMN addon_mod_id INTEGER")

    mod_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(mods)").fetchall()}
    if "parent_mod_id" not in mod_columns:
        sync_conn.exec_driver_sql("ALTER TABLE mods ADD COLUMN parent_mod_id INTEGER")
    if "catalog_visible" not in mod_columns:
        sync_conn.exec_driver_sql("ALTER TABLE mods ADD COLUMN catalog_visible BOOLEAN NOT NULL DEFAULT 1")
    if "origin" not in mod_columns:
        sync_conn.exec_driver_sql("ALTER TABLE mods ADD COLUMN origin VARCHAR(30) NOT NULL DEFAULT 'discord'")

    build_mod_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(build_mods)").fetchall()}
    if "auto_added" not in build_mod_columns:
        sync_conn.exec_driver_sql("ALTER TABLE build_mods ADD COLUMN auto_added BOOLEAN NOT NULL DEFAULT 0")
    if "selected_file_url" not in build_mod_columns:
        sync_conn.exec_driver_sql("ALTER TABLE build_mods ADD COLUMN selected_file_url TEXT")
    if "selected_file_name" not in build_mod_columns:
        sync_conn.exec_driver_sql("ALTER TABLE build_mods ADD COLUMN selected_file_name VARCHAR(500)")

    cache_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(cached_files)").fetchall()}
    if "owner_mod_id" not in cache_columns:
        sync_conn.exec_driver_sql("ALTER TABLE cached_files ADD COLUMN owner_mod_id INTEGER")
    if "owner_release_id" not in cache_columns:
        sync_conn.exec_driver_sql("ALTER TABLE cached_files ADD COLUMN owner_release_id INTEGER")
    if "game_version" not in cache_columns:
        sync_conn.exec_driver_sql("ALTER TABLE cached_files ADD COLUMN game_version VARCHAR(80)")

    build_columns = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(builds)").fetchall()}
    if "name" not in build_columns:
        sync_conn.exec_driver_sql(
            "ALTER TABLE builds ADD COLUMN name VARCHAR(160) NOT NULL DEFAULT 'Vintage Story Modpack'"
        )
    if "schema_version" not in build_columns:
        sync_conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def _backfill_canonical_ids() -> None:
    """Populate canonical forum-title identities for catalogs created before this field existed."""
    from sqlalchemy import select

    from .models import DiscordSource, Mod
    from .services.identity import canonical_identifier

    async with SessionLocal() as session:
        mods = (await session.execute(select(Mod).order_by(Mod.id))).scalars().all()
        sources = (await session.execute(select(DiscordSource).order_by(DiscordSource.id))).scalars().all()
        title_by_mod: dict[int, str] = {}
        for source in sources:
            if source.mod_id and source.post_title and source.mod_id not in title_by_mod:
                title_by_mod[source.mod_id] = source.post_title
        changed = False
        for mod in mods:
            if mod.canonical_id:
                continue
            seed = title_by_mod.get(mod.id) if mod.origin == "discord" else mod.mod_id
            key = canonical_identifier(seed)
            if key:
                mod.canonical_id = key
                changed = True
        if changed:
            await session.commit()


async def _repoint_relation_edges(session, model, duplicate_id: int, canonical_id: int) -> None:
    """Repoint edges without transiently violating the model's unique key.

    SQLAlchemy autoflushes pending UPDATEs before a SELECT. Updating one edge to its canonical
    target before checking the next edge could therefore collide with an edge that is already
    at that target. Plan all final keys first, merge collisions, flush deletes, then apply moves.
    NULL targets are intentionally not grouped: multiple unresolved dependency names are valid.
    """
    from sqlalchemy import select
    from .models import Mod

    rows = (await session.execute(select(model).order_by(model.id))).scalars().all()
    affected_ids = {
        row.id for row in rows
        if row.mod_id == duplicate_id or row.target_mod_id == duplicate_id
    }
    if not affected_ids:
        return

    destinations = {}
    groups = {}
    for row in rows:
        new_source = canonical_id if row.mod_id == duplicate_id else row.mod_id
        new_target = canonical_id if row.target_mod_id == duplicate_id else row.target_mod_id
        destinations[row.id] = (new_source, new_target)
        if new_target is not None:
            groups.setdefault((new_source, new_target, row.relation_type), []).append(row)

    # A group matters only when at least one edge points to/from the duplicate. All rows that
    # will share a non-NULL target key are merged together, including rows already at the target.
    affected_groups = [
        group for group in groups.values() if any(row.id in affected_ids for row in group)
    ]
    survivors_to_move = {}
    for group in affected_groups:
        # Prefer a row already at its final key to minimize writes and avoid UNIQUE transient states.
        survivor = min(
            group,
            key=lambda row: (
                1 if row.id in affected_ids else 0,
                row.id,
            ),
        )
        for other in group:
            if other is survivor:
                continue
            # Preserve the strongest and most complete relation evidence.
            survivor.confidence = max(float(survivor.confidence or 0), float(other.confidence or 0))
            survivor.verified = bool(survivor.verified or other.verified)
            if hasattr(survivor, "required_version") and not survivor.required_version and other.required_version:
                survivor.required_version = other.required_version
            for field in ("target_name", "target_url", "source_kind", "raw_phrase", "evidence"):
                if not getattr(survivor, field, None) and getattr(other, field, None):
                    setattr(survivor, field, getattr(other, field))
            await session.delete(other)
        final_source, final_target = destinations[survivor.id]
        survivors_to_move[survivor.id] = (survivor, final_source, final_target)

    # NULL-target edges don't participate in the SQLite UNIQUE constraint in the same way and
    # may represent distinct unresolved mod IDs, so preserve each one independently.
    for row in rows:
        if row.id not in affected_ids or row.target_mod_id is not None:
            continue
        destinations[row.id] = (
            canonical_id if row.mod_id == duplicate_id else row.mod_id,
            None,
        )
        survivors_to_move[row.id] = (row, *destinations[row.id])

    # Free unique keys before writing final target IDs. This is the critical ordering fix.
    await session.flush()
    for row, new_source, new_target in survivors_to_move.values():
        row.mod_id = new_source
        row.target_mod_id = new_target
        # Keep human-readable target metadata aligned with the now-canonical target.
        if new_target == canonical_id:
            canonical = await session.get(Mod, canonical_id)
            if canonical is not None:
                row.target_name = row.target_name or canonical.name
                row.target_url = row.target_url or canonical.mod_db_url
    await session.flush()


async def _dedupe_dependency_mods() -> None:
    """Merge duplicate dependency-only records while preserving builds, releases and relations.

    Only `origin=dependency` rows are eligible for merging. Two independently curated Discord
    posts are never merged just because their titles or modinfo identifiers happen to match.
    """
    from sqlalchemy import delete, func, select

    from .models import (
        Addon, BuildMod, CachedFile, Compatibility, Dependency, DiscordSource, Mod, ModRelease,
    )
    from .services.identity import canonical_identifier, is_core_mod_identifier

    async with SessionLocal() as session:
        mods = (await session.execute(select(Mod).order_by(Mod.id))).scalars().all()
        for mod in mods:
            if is_core_mod_identifier(mod.mod_id) and mod.origin == "dependency":
                mod.catalog_visible = False

        mods = (await session.execute(select(Mod).order_by(Mod.id))).scalars().all()
        by_id = {m.id: m for m in mods}

        def keys(mod):
            identity_keys = {canonical_identifier(mod.mod_id), canonical_identifier(mod.canonical_id)} - {""}
            # Some Mod DB aliases/forks expose a changed modinfo identifier while retaining the
            # same API/page identity. These numeric keys deduplicate repeated imports of the same
            # library, but are deliberately not used to collapse distinct curated Discord entries.
            if mod.mod_db_id is not None:
                identity_keys.add(f"api{int(mod.mod_db_id)}")
            if mod.mod_db_asset_id is not None:
                identity_keys.add(f"asset{int(mod.mod_db_asset_id)}")
            return identity_keys

        # Find a curated Discord record for each dependency-only record by either exact modinfo
        # ID or canonical forum title. If none exists, keep the earliest dependency record per key.
        dep_rows = [m for m in mods if m.origin == "dependency" and m.mod_id and not is_core_mod_identifier(m.mod_id)]
        processed: set[int] = set()
        for dep in dep_rows:
            if dep.id in processed:
                continue
            dep_keys = keys(dep)
            matches = [m for m in mods if m.id != dep.id and dep_keys.intersection(keys(m))]
            curated = [m for m in matches if m.origin != "dependency"]
            exact_curated = [m for m in curated if (
                canonical_identifier(m.mod_id) in dep_keys
                or (dep.mod_db_id is not None and m.mod_db_id == dep.mod_db_id)
                or (dep.mod_db_asset_id is not None and m.mod_db_asset_id == dep.mod_db_asset_id)
            )]
            # A curated forum post with a matching canonical title but a *different* modinfo
            # identifier is an intentional fork/fix override, not a duplicate of the original.
            # Never merge the original's releases/cache into that fixed mod.
            alias_curated = [
                m for m in curated
                if canonical_identifier(m.canonical_id) in dep_keys
                and m not in exact_curated
                and m.catalog_visible
            ]
            if not exact_curated and len(alias_curated) == 1:
                alias = alias_curated[0]
                await _repoint_relation_edges(session, Dependency, dep.id, alias.id)
                await session.execute(delete(Dependency).where(Dependency.mod_id == dep.id))
                dep.catalog_visible = False
                dep.origin = "superseded_dependency"
                processed.add(dep.id)
                logger.info(
                    "[DB] kept old dependency record %s hidden; canonical Discord entry %s points to a different modinfo ID",
                    dep.mod_id, alias.name,
                )
                continue
            if not exact_curated and len(alias_curated) > 1:
                logger.error(
                    "[DB] multiple Discord title overrides for dependency %s; will leave resolution to prefetch validation",
                    dep.mod_id,
                )
            if exact_curated:
                canonical = sorted(exact_curated, key=lambda m: m.id)[0]
            else:
                duplicate_dependencies = [m for m in matches if m.origin == "dependency"]
                canonical = min([dep, *duplicate_dependencies], key=lambda m: m.id)
            group = [m for m in matches if m.origin == "dependency" and m.id != canonical.id]
            if dep.id != canonical.id and dep not in group:
                group.append(dep)
            for duplicate in sorted({m.id: m for m in group}.values(), key=lambda m: m.id):
                if duplicate.id == canonical.id or duplicate.id in processed:
                    continue

                # Move/map release rows first, preserving BuildMod references by internal release PK.
                canonical_releases = (
                    await session.execute(select(ModRelease).where(ModRelease.mod_id == canonical.id))
                ).scalars().all()
                old_releases = (
                    await session.execute(select(ModRelease).where(ModRelease.mod_id == duplicate.id))
                ).scalars().all()
                release_map: dict[int, int] = {}
                for old in old_releases:
                    target_release = next((r for r in canonical_releases if r.release_id == old.release_id), None)
                    if target_release is None:
                        target_release = next((r for r in canonical_releases if
                            (r.mod_version and r.mod_version == old.mod_version and r.filename == old.filename) or
                            (r.file_url and r.file_url == old.file_url)), None)
                    if target_release is not None:
                        release_map[old.id] = target_release.id
                    else:
                        used_release_ids = {r.release_id for r in canonical_releases}
                        if old.release_id in used_release_ids:
                            old.release_id = -max(1, old.id)
                            while old.release_id in used_release_ids:
                                old.release_id -= 1
                        old.mod_id = canonical.id
                        canonical_releases.append(old)
                        release_map[old.id] = old.id

                # Remap legacy build references, avoiding the BuildMod(build_id, mod_id) unique key.
                duplicate_build_rows = (
                    await session.execute(select(BuildMod).where(BuildMod.mod_id == duplicate.id))
                ).scalars().all()
                for bm in duplicate_build_rows:
                    old_release_id = bm.release_id
                    if old_release_id in release_map:
                        bm.release_id = release_map[old_release_id]
                    existing_bm = (await session.execute(select(BuildMod).where(
                        BuildMod.build_id == bm.build_id, BuildMod.mod_id == canonical.id
                    ).limit(1))).scalars().first()
                    if existing_bm:
                        if existing_bm.release_id is None and bm.release_id is not None:
                            existing_bm.release_id = bm.release_id
                            existing_bm.selected_version = existing_bm.selected_version or bm.selected_version
                        existing_bm.selected_file_url = existing_bm.selected_file_url or bm.selected_file_url
                        existing_bm.selected_file_name = existing_bm.selected_file_name or bm.selected_file_name
                        existing_bm.auto_added = bool(existing_bm.auto_added or bm.auto_added)
                        existing_bm.position = min(existing_bm.position, bm.position)
                        await session.delete(bm)
                    else:
                        bm.mod_id = canonical.id

                for cached in (await session.execute(select(CachedFile).where(CachedFile.owner_mod_id == duplicate.id))).scalars().all():
                    cached.owner_mod_id = canonical.id
                    if cached.owner_release_id in release_map:
                        cached.owner_release_id = release_map[cached.owner_release_id]

                for source in (await session.execute(select(DiscordSource).where(DiscordSource.mod_id == duplicate.id))).scalars().all():
                    source.mod_id = canonical.id
                for addon in (await session.execute(select(Addon).where(
                    (Addon.mod_id == duplicate.id) | (Addon.addon_mod_id == duplicate.id)
                ))).scalars().all():
                    if addon.mod_id == duplicate.id:
                        addon.mod_id = canonical.id
                    if addon.addon_mod_id == duplicate.id:
                        addon.addon_mod_id = canonical.id
                for child in (await session.execute(select(Mod).where(Mod.parent_mod_id == duplicate.id))).scalars().all():
                    child.parent_mod_id = canonical.id

                # Re-point dependency/compatibility edges in two phases so pending UPDATEs cannot
                # autoflush into a UNIQUE(mod_id, target_mod_id, relation_type) collision.
                for model in (Dependency, Compatibility):
                    await _repoint_relation_edges(session, model, duplicate.id, canonical.id)

                # Merge only missing metadata; the preferred canonical record remains authoritative.
                for field in ("mod_db_id", "mod_db_asset_id", "mod_id", "canonical_id", "description",
                              "short_description", "image_url", "image_cache_path", "author", "mod_type",
                              "mod_db_url", "latest_file_url", "latest_file_name", "latest_game_version"):
                    if not getattr(canonical, field, None) and getattr(duplicate, field, None):
                        setattr(canonical, field, getattr(duplicate, field))
                canonical.catalog_visible = bool(canonical.catalog_visible or duplicate.catalog_visible)
                await session.delete(duplicate)
                processed.add(duplicate.id)

        # Remove legacy dependency edges to vanilla core components. They are bundled with the game
        # and are not downloadable modpack entries, even though modinfo uses those dependency IDs.
        core_mods = (await session.execute(select(Mod).where(
            func.lower(Mod.mod_id).in_(["game", "survival", "creative"])
        ))).scalars().all()
        core_ids = [m.id for m in core_mods]
        if core_ids:
            await session.execute(delete(Dependency).where(
                (Dependency.target_mod_id.in_(core_ids)) | func.lower(Dependency.target_name).in_(["game", "survival", "creative"])
            ))
            await session.execute(delete(BuildMod).where(BuildMod.mod_id.in_(core_ids)))
            await session.execute(delete(Dependency).where(Dependency.mod_id.in_(core_ids)))
            for core in core_mods:
                core.catalog_visible = False
        await session.commit()


def _create_partial_indexes(sync_conn) -> None:
    # Application-level per-ID locks protect the upsert path; these indexes are the final guard
    # against a race that tries to insert the same dependency-only library twice.
    sync_conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dependency_mod_identifier "
        "ON mods(lower(mod_id)) WHERE origin = 'dependency' AND mod_id IS NOT NULL"
    )
    sync_conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dependency_canonical_identifier "
        "ON mods(lower(canonical_id)) WHERE origin = 'dependency' AND canonical_id IS NOT NULL"
    )
