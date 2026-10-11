from __future__ import annotations

import re
import unicodedata

CORE_MOD_IDS = frozenset({"game", "survival", "creative"})
DEFAULT_AUTO_LIBRARY_IDS = frozenset({"imgui", "libgui", "musicvolumebalancer"})


def canonical_identifier(value: str | None) -> str:
    """Compact a modinfo identifier or Discord forum title into a stable comparison key."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def is_core_mod_identifier(value: str | None) -> bool:
    return canonical_identifier(value) in CORE_MOD_IDS


def is_library_dependency(
    identifier: str,
    *,
    name: str | None = None,
    description: str | None = None,
    mod_type: str | None = None,
    configured_ids: set[str] | frozenset[str] | None = None,
) -> bool:
    """Conservative positive classifier. Unknown/content mods must be requested in Discord,
    not silently imported as dependency-only catalog entries.
    """
    key = canonical_identifier(identifier)
    if not key or key in CORE_MOD_IDS:
        return False
    allow = set(DEFAULT_AUTO_LIBRARY_IDS)
    allow.update(canonical_identifier(x) for x in (configured_ids or set()))
    if key in allow:
        return True

    name_key = canonical_identifier(name)
    type_key = canonical_identifier(mod_type)
    # The name is more useful than Mod DB's generic code/content type. These are strong
    # library indicators, not mere mentions in the full description.
    strong_name_markers = (
        "library", "libraries", "libgui", "imgui", "framework", "modapi",
        "sharedlib", "commonlib", "musicvolumebalancer", "toolkit",
    )
    if any(marker in name_key for marker in strong_name_markers) or name_key.endswith("lib"):
        return True
    if type_key in {"library", "framework", "api"}:
        return True
    # Do not infer "library" from arbitrary identifier prefixes (e.g. "apiary" is a
    # content mod). Unknown IDs are deliberately sent to the manual Discord request flow.
    # Descriptions are considered only for explicit phrases; arbitrary occurrences of "API"
    # in a usage paragraph are not enough to classify a content mod as a library.
    desc = re.sub(r"\s+", " ", str(description or "")).casefold()
    explicit_phrases = (
        "modding library", "shared library", "library for mod authors",
        "library mod", "framework for mods", "api library", "provides an api for other mods",
    )
    return any(phrase in desc for phrase in explicit_phrases)
