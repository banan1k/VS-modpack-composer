import asyncio
import json
import zipfile
from pathlib import Path

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

