import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
import ast

import pytest

from app.services.modinfo import read_modinfo, read_dependencies
from app.services.priority import PriorityCoordinator


def test_read_modinfo_dependencies(tmp_path: Path):
    path = tmp_path / "Steelmaking.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("modinfo.json", json.dumps({
            "name": "Steelmaking Expanded",
            "modid": "smex",
            "dependencies": {"game": "1.22.0", "exlib": "0.8.4", "ppex": "0.7.1"},
        }))
    data = read_modinfo(path)
    deps = read_dependencies(path)
    assert deps == {"exlib": "0.8.4", "ppex": "0.7.1"}


def test_priority_coordinator_pauses_background_for_user():
    async def scenario():
        gate = PriorityCoordinator()
        bg_started = asyncio.Event()
        bg_finish = asyncio.Event()
        user_started = asyncio.Event()

        async def bg():
            async with gate.background_slot():
                bg_started.set()
                await bg_finish.wait()

        bg_task = asyncio.create_task(bg())
        await bg_started.wait()
        user_task = asyncio.create_task(_user(gate, user_started))
        await asyncio.sleep(0.03)
        assert not user_started.is_set()
        bg_finish.set()
        await asyncio.wait_for(user_task, 1)
        await asyncio.wait_for(bg_task, 1)
        assert user_started.is_set()

    async def _user(gate, event):
        async with gate.user_priority():
            event.set()

    asyncio.run(scenario())


def test_local_release_cap_check_passes_release_object_to_matcher():
    source = Path("app/services/prefetch.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_has_local_release_for_cap")
    namespace = {"Mod": object, "_release_matches_cap": lambda release, cap: (
        hasattr(release, "game_versions_json") and cap == "1.22.7"
    )}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<test_prefetch_helper>", "exec"), namespace)

    release = SimpleNamespace(
        game_versions_json=json.dumps(["1.22.0 - 1.22.7"]),
        file_url="https://example.invalid/mod.zip",
    )
    mod = SimpleNamespace(releases=[release])
    assert namespace["_has_local_release_for_cap"](mod, "1.22.7") is True
    assert namespace["_has_local_release_for_cap"](mod, "1.23.0") is False


def test_upsert_exact_dependency_creates_dependency_mod_without_lazy_async_io(tmp_path, monkeypatch):
    pytest.importorskip("aiosqlite")
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import Dependency, Mod, ModRelease
    from app.services import prefetch as prefetch_module
    from app.services.moddb import ModDBData

    class FakeModDB:
        site_base = "https://mods.vintagestory.at"

        async def get_catalog_rows(self):
            return []

        async def get_mod(self, identifier, source_url=None, catalog_rows=None):
            assert identifier == "exlib"
            return ModDBData(
                mod_db_id=1234,
                asset_id=2345,
                mod_id="exlib",
                name="Expanded Library",
                description="A test dependency library.",
                short_description="A test dependency library.",
                image_url=None,
                author="Test Author",
                mod_type="code",
                mod_db_url="https://mods.vintagestory.at/exlib",
                releases=[{
                    "releaseid": 9876,
                    "modversion": "0.8.4",
                    "filename": "exlib_0.8.4.zip",
                    "fileid": 9876,
                    "mainfile": "https://example.invalid/exlib_0.8.4.zip",
                    "tags": ["1.22.0 - 1.22.7"],
                }],
                source_updated_at=None,
                raw_json="{}",
                notes=[],
            )

    async def scenario():
        db_path = tmp_path / "prefetch-dependency-test.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(prefetch_module, "SessionLocal", session_factory)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with session_factory() as session:
                source = Mod(
                    mod_db_id=1,
                    mod_db_asset_id=1,
                    mod_id="smex",
                    name="Steelmaking Expanded",
                    mod_db_url="https://mods.vintagestory.at/smex",
                    catalog_visible=True,
                    origin="discord",
                )
                session.add(source)
                await session.commit()
                source_pk = source.id

            manager = prefetch_module.PrefetchManager(
                cache=object(),
                moddb=FakeModDB(),
                coordinator=prefetch_module.PriorityCoordinator(),
            )
            item = prefetch_module.PrefetchItem(
                mod_id=source_pk,
                mod_identifier="smex",
                game_version="1.22.7",
                file_url="https://example.invalid/smex.zip",
                filename="smex.zip",
                release_id=111,
                mod_version="0.8.4",
            )
            await manager._upsert_exact_dependency(item, "exlib", "0.8.4")

            async with session_factory() as session:
                target = (await session.execute(select(Mod).where(Mod.mod_id == "exlib"))).scalars().one()
                release = (await session.execute(select(ModRelease).where(ModRelease.mod_id == target.id))).scalars().one()
                dependency = (
                    await session.execute(
                        select(Dependency).where(
                            Dependency.mod_id == source_pk,
                            Dependency.target_mod_id == target.id,
                            Dependency.relation_type == "dependency",
                        )
                    )
                ).scalars().one()
                assert target.name == "Expanded Library"
                assert target.latest_game_version is not None
                assert release.mod_version == "0.8.4"
                assert dependency.required_version == "0.8.4"
                assert dependency.source_kind == "modinfo"
                assert dependency.confidence == 1.0
                assert dependency.verified is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_prefetch_imports_version_compatibility_helper():
    source = Path("app/services/prefetch.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "versioning"
        for alias in node.names
    }
    assert "is_version_compatible" in imported_names
