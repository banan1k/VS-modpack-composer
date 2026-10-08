from __future__ import annotations

import re

SEMVER_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?:[-+][0-9A-Za-z.-]+)?")


def version_key(v: str) -> tuple[int, int, int, int, int, str]:
    text = str(v or "")
    nums = [int(x) for x in re.findall(r"\d+", text)[:4]]
    nums += [0] * (4 - len(nums))
    prerelease = 0 if re.search(r"(?:pre|rc|alpha|beta|dev)", text, re.I) else 1
    return (nums[0], nums[1], nums[2], nums[3], prerelease, text.lower())


def numeric_version_key(v: str) -> tuple[int, int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", str(v or ""))[:4]]
    nums += [0] * (4 - len(nums))
    return tuple(nums[:4])  # type: ignore[return-value]


def game_major_minor(v: str) -> tuple[int, int] | None:
    m = SEMVER_RE.search(str(v or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def supported_game_versions(tags: list[str] | tuple[str, ...]) -> list[tuple[int, int, int, str]]:
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


def _tag_covers_cap(raw: str, cap: str) -> bool:
    versions = list(SEMVER_RE.finditer(str(raw or "")))
    if len(versions) < 2:
        return False
    cap_key = numeric_version_key(cap)
    parsed = [
        (
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3) or 0),
        )
        for m in versions[:2]
    ]
    lo, hi = sorted(parsed)
    return lo <= cap_key[:3] <= hi and (lo[0], lo[1]) == (cap_key[0], cap_key[1])


def latest_game_version(tags: list[str], cap: str | None = None) -> str | None:
    versions = supported_game_versions(tags)
    if not versions:
        return None

    if cap:
        cap_key = numeric_version_key(cap)
        candidates = [v for v in versions if (v[0], v[1], v[2]) <= cap_key[:3]]
        # A range such as 1.22.0 - 1.22.7 should display 1.22.6 when the configured
        # catalog ceiling is 1.22.6 even though that exact patch is not an endpoint tag.
        if any(_tag_covers_cap(str(raw), cap) for raw in tags):
            candidates.append((cap_key[0], cap_key[1], cap_key[2], cap))
        if candidates:
            best = max(candidates, key=lambda x: (x[0], x[1], x[2]))
            return f"{best[0]}.{best[1]}.{best[2]}"

    best = max(versions, key=lambda x: (x[0], x[1], x[2]))
    return f"{best[0]}.{best[1]}.{best[2]}"


def is_version_compatible(release_tags: list[str], target: str) -> bool:
    wanted = game_major_minor(target)
    if wanted is None:
        return False
    return any((major, minor) == wanted for major, minor, _patch, _raw in supported_game_versions(release_tags))



def is_release_compatible_with_cap(release_tags: list[str] | tuple[str, ...], cap: str) -> bool:
    """Return True when a release supports the same major.minor branch without exceeding cap.

    A tag can be a point (1.22.6) or a range (1.22.0 - 1.22.7). Ranges are treated as covering
    every patch in between; point tags must be <= the configured ceiling.
    """
    cap_nums = numeric_version_key(cap)
    if len(cap_nums) < 2:
        return False
    cap3 = cap_nums[:3]
    want_branch = (cap3[0], cap3[1])
    for raw in release_tags:
        matches = list(SEMVER_RE.finditer(str(raw or "")))
        if not matches:
            continue
        parsed = [
            (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
            for m in matches
        ]
        if len(parsed) >= 2:
            lo, hi = sorted(parsed[:2])
            if (lo[0], lo[1]) == want_branch and (hi[0], hi[1]) == want_branch and lo <= cap3 <= hi:
                return True
        else:
            point = parsed[0]
            if (point[0], point[1]) == want_branch and point <= cap3:
                return True
    return False


def choose_latest_release(releases: list[dict], target: str | None) -> dict | None:
    candidates = []
    for r in releases:
        tags = r.get("tags") or r.get("gameversions") or r.get("game_versions") or []
        if target is None or is_version_compatible([str(x) for x in tags], target):
            candidates.append(r)
    candidates.sort(
        key=lambda r: (version_key(str(r.get("modversion", "0"))), str(r.get("created", ""))),
        reverse=True,
    )
    return candidates[0] if candidates else None
