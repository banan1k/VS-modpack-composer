from __future__ import annotations

import re

SEMVER_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?:[-+][0-9A-Za-z.-]+)?")


def version_key(v: str) -> tuple[int, int, int, int, int, str]:
    """Return a fixed-shape, always-comparable version sort key.

    A previous implementation returned a variable-length tuple. For example,
    ``1.22.7`` produced ``(1, 22, 7, ...)`` while ``1.22`` produced
    ``(1, 22, ..., ...)``. Comparing those tuples eventually compared an int
    with a string and crashed catalog synchronization for otherwise valid mods.
    """
    text = str(v or "")
    nums = [int(x) for x in re.findall(r"\d+", text)[:4]]
    nums += [0] * (4 - len(nums))
    prerelease = 0 if re.search(r"(?:pre|rc|alpha|beta|dev)", text, re.I) else 1
    return (nums[0], nums[1], nums[2], nums[3], prerelease, text.lower())


def game_major_minor(v: str) -> tuple[int, int] | None:
    m = SEMVER_RE.search(str(v or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def supported_game_versions(tags: list[str] | tuple[str, ...]) -> list[tuple[int, int, int, str]]:
    """Extract all game-version mentions from tags, including range tags."""
    out: list[tuple[int, int, int, str]] = []
    seen: set[tuple[int, int, int, str]] = set()
    for raw in tags:
        text = str(raw or "")
        for match in SEMVER_RE.finditer(text):
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3) or 0)
            value = match.group(0)
            item = (major, minor, patch, value)
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def is_version_compatible(release_tags: list[str], target: str) -> bool:
    """Compatibility is intentionally checked at major/minor precision.

    Thus a release tagged `1.22.0 - 1.22.6` is considered compatible with a build
    target `1.22.7`, because both belong to the same 1.22 game branch.
    """
    wanted = game_major_minor(target)
    if wanted is None:
        return False
    return any((major, minor) == wanted for major, minor, _patch, _raw in supported_game_versions(release_tags))


def choose_latest_release(releases: list[dict], target: str | None) -> dict | None:
    candidates = []
    for r in releases:
        tags = r.get("tags") or r.get("gameversions") or []
        if target is None or is_version_compatible([str(x) for x in tags], target):
            candidates.append(r)
    candidates.sort(
        key=lambda r: (version_key(str(r.get("modversion", "0"))), str(r.get("created", ""))),
        reverse=True,
    )
    return candidates[0] if candidates else None


def latest_game_version(tags: list[str]) -> str | None:
    versions = supported_game_versions(tags)
    if not versions:
        return None
    # Game-version tags are compared only by numeric major/minor/patch.
    # Do not compare raw suffixes here: Mod DB tags can have mixed precision
    # and suffixes, and raw tuple shapes must never make synchronization fail.
    best = max(versions, key=lambda x: (x[0], x[1], x[2]))
    return f"{best[0]}.{best[1]}.{best[2]}"
