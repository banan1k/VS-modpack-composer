from __future__ import annotations

import json
import zipfile
from pathlib import Path


def read_modinfo(path: Path) -> dict:
    """Read the nearest modinfo.json from a Vintage Story mod archive."""
    with zipfile.ZipFile(path, "r") as zf:
        candidates = [name for name in zf.namelist() if Path(name).name.casefold() == "modinfo.json"]
        if not candidates:
            raise FileNotFoundError("modinfo.json not found")
        candidates.sort(key=lambda name: (name.count("/"), len(name)))
        raw = zf.read(candidates[0])
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("modinfo.json is not an object")
    return payload


def read_dependencies(path: Path) -> dict[str, str]:
    data = read_modinfo(path)
    deps = data.get("dependencies")
    if not isinstance(deps, dict):
        return {}
    return {
        str(key).strip(): str(value or "").strip()
        for key, value in deps.items()
        if str(key).strip() and str(key).casefold() != "game"
    }
