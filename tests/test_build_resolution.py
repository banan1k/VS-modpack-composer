import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

from app.services.versioning import is_version_compatible, version_key

ROOT = Path(__file__).parents[1]


def _function(source_path, name, namespace):
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    candidates = list(tree.body)
    for item in tree.body:
        if isinstance(item, ast.ClassDef):
            candidates.extend(item.body)
    node = next(
        item for item in candidates
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace[name]


def test_v2_install_information_is_indexed_by_requested_and_returned_identifier():
    namespace = {}
    moddb = ROOT / "app/services/builds.py"
    _function(moddb, "_info_value", namespace)
    install_info_map = _function(moddb, "_install_info_map", namespace)
    payload = {
        "data": {
            "FixedPaths": {
                "modidstr": "fixedpaths",
                "fileName": "FixedPaths_1.22.zip",
                "fileUrl": "https://cdn.example/fixedpaths.zip",
            }
        }
    }
    result = install_info_map(payload)
    assert result["fixedpaths"]["fileName"] == "FixedPaths_1.22.zip"
    assert result["fixedpaths"]["fileUrl"].endswith("fixedpaths.zip")


def test_build_release_selection_ignores_rows_without_downloadable_file():
    namespace = {
        "json": json,
        "version_key": version_key,
        "is_version_compatible": is_version_compatible,
    }
    release_for_game = _function(ROOT / "app/services/builds.py", "_release_for_game", namespace)
    releases = [
        SimpleNamespace(
            id=1,
            mod_version="0.4.0",
            file_url=None,
            created_at="2026-01-01",
            game_versions_json='["1.22.0 - 1.22.7"]',
        ),
        SimpleNamespace(
            id=2,
            mod_version="0.3.2",
            file_url="https://cdn.example/mod-0.3.2.zip",
            created_at="2025-01-01",
            game_versions_json='["1.22.0 - 1.22.7"]',
        ),
    ]
    assert release_for_game(releases, "1.22.7").mod_version == "0.3.2"


def test_cache_folder_is_human_readable_and_unique_by_mod_identifier():
    namespace = {"re": re}
    cache_file = ROOT / "app/services/cache.py"
    folder_component = _function(cache_file, "_folder_component", namespace)
    folder = folder_component("Fixed Paths", "fixedpaths", 42)
    assert "Fixed-Paths" in folder
    assert "fixedpaths" in folder
    assert folder != "42"


def test_discord_publish_only_attaches_json_but_keeps_legacy_txt_recovery():
    main_source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'filename="modpack.json"' in main_source
    assert 'filename="modpack.txt"' not in main_source
    # TXT remains a supported input to restore older published builds.
    assert 'attachment.filename.casefold() == "modpack.txt"' in main_source


def test_existing_buildmods_have_migration_for_v2_selected_file_url_and_name():
    db_source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    assert 'ALTER TABLE build_mods ADD COLUMN selected_file_url TEXT' in db_source
    assert 'ALTER TABLE build_mods ADD COLUMN selected_file_name VARCHAR(500)' in db_source


def test_latest_downloadable_release_can_be_used_after_game_tag_warning():
    namespace = {"version_key": version_key}
    latest_downloadable = _function(ROOT / "app/services/builds.py", "_latest_downloadable_release", namespace)
    releases = [
        SimpleNamespace(
            id=1, mod_version="1.0.1", file_url="https://cdn.example/FixedPaths.zip",
            filename="FixedPaths.zip", created_at="2025-01-01",
            game_versions_json='["1.20.0 - 1.20.1"]',
        ),
        SimpleNamespace(
            id=2, mod_version="1.0.2", file_url="https://cdn.example/FixedPaths.zip",
            filename="FixedPaths.zip", created_at="2025-01-23",
            game_versions_json='["1.20.0 - 1.20.1", "1.21.1"]',
        ),
    ]
    # Neither release is tagged 1.22.7, but when a user confirms the warning the latest
    # downloadable file should be used instead of failing with an empty URL.
    assert latest_downloadable(releases).mod_version == "1.0.2"


def test_build_resolution_has_latest_v2_release_fallback_for_untagged_mods():
    source = (ROOT / "app/services/builds.py").read_text(encoding="utf-8")
    assert 'self.moddb.get_latest_release(item["mod_db_id"], identifier=item["mod_identifier"])' in source
    assert 'self.moddb.get_latest_release(mod.mod_db_id, identifier=mod.mod_id)' in source
    assert "using out-of-tag release" in source


async def test_existing_build_recovers_url_from_latest_release_when_gv_install_info_has_no_file(monkeypatch):
    import pytest
    pytest.importorskip("aiosqlite")

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import app.services.builds as builds_module
    from app.db import Base
    from app.models import Build, BuildMod, Mod, ModRelease
    from app.services.builds import BuildService

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    test_sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(builds_module, "SessionLocal", test_sessions)

    class FakeModDB:
        site_base = "https://mods.vintagestory.at"

        async def get_install_information(self, specs, game_version):
            return {"data": {"fixedpaths": {"errorCode": 4031}}}

        async def get_latest_release(self, mod_db_id, identifier=None):
            assert mod_db_id == 1896
            assert identifier == "fixedpaths"
            return {
                "releaseId": 98765,
                "identifier": "fixedpaths",
                "version": "1.0.2",
                "fileName": "FixedPaths.zip",
                "fileUrl": "/download/98765/FixedPaths.zip",
            }

    async with test_sessions() as session:
        mod = Mod(
            mod_db_id=1896,
            mod_db_asset_id=10236,
            mod_id="fixedpaths",
            canonical_id="fixedpaths",
            name="Fixed Paths",
            mod_db_url="https://mods.vintagestory.at/show/mod/10236",
            supported_versions_json='["1.20.0 - 1.20.1"]',
            parse_status="ok",
            origin="discord",
            catalog_visible=True,
        )
        session.add(mod)
        await session.flush()
        release = ModRelease(
            mod_id=mod.id,
            release_id=123,
            mod_version="1.0.2",
            filename="FixedPaths.zip",
            file_url=None,
            game_versions_json='["1.20.0 - 1.20.1"]',
        )
        session.add(release)
        await session.flush()
        build = Build(
            share_id="fixed-paths-test",
            name="Fixed Paths regression",
            target_game_version="1.22.7",
            published=False,
        )
        session.add(build)
        await session.flush()
        build_mod = BuildMod(
            build_id=build.id,
            mod_id=mod.id,
            release_id=release.id,
            selected_version="1.0.2",
            selected_file_url=None,
            selected_file_name="FixedPaths.zip",
            auto_added=False,
            position=0,
        )
        session.add(build_mod)
        await session.commit()

    service = BuildService(cache=None, moddb_client=FakeModDB())
    items = await service._resolve_build_file_items("fixed-paths-test")
    assert len(items) == 1
    assert items[0]["url"] == "https://mods.vintagestory.at/download/98765/FixedPaths.zip"
    assert items[0]["filename"] == "FixedPaths.zip"
    async with test_sessions() as session:
        saved = await session.get(BuildMod, 1)
        assert saved is not None
        assert saved.selected_file_url == items[0]["url"]
        assert saved.selected_file_name == "FixedPaths.zip"

    await engine.dispose()
