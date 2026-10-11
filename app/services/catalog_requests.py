"""Helpers for safe, deduplicated requests to add relation targets to the catalog."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote, urlsplit

from .identity import canonical_identifier


def normalized_moddb_page_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parsed.path or "").rstrip("/").lower()
    return f"{host}{path}" if host and path else ""


def is_moddb_page_url(url: str | None) -> bool:
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in {"mods.vintagestory.at", "www.mods.vintagestory.at"}:
        return False
    if parsed.scheme not in {"https", "http"}:
        return False
    path = unquote(parsed.path or "").strip("/").lower()
    if not path or path.startswith(("api/", "download/", "show/user/", "list/", "versionchecker")):
        return False
    return path.startswith("show/mod/") or (
        "/" not in path and bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,119}", path))
    )


def catalog_request_key(url: str) -> str:
    normalized = normalized_moddb_page_url(url)
    return "web-request:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:48]


def find_catalog_mod(
    target_name: str | None,
    target_url: str | None,
    target_id: int | None,
    catalog_mods,
):
    """Resolve a relation target against visible catalog rows, tolerating URL formats from Mod DB."""
    if target_id is not None:
        for mod in catalog_mods:
            if mod.id == target_id:
                return mod

    normalized_url = normalized_moddb_page_url(target_url)
    if normalized_url:
        for mod in catalog_mods:
            if normalized_moddb_page_url(mod.mod_db_url) == normalized_url:
                return mod
        path = urlsplit(str(target_url or "")).path.strip("/")
        asset_match = re.fullmatch(r"show/mod/(\d+)", unquote(path), re.I)
        if asset_match:
            asset_id = int(asset_match.group(1))
            for mod in catalog_mods:
                if mod.mod_db_asset_id == asset_id:
                    return mod
        elif path and "/" not in path:
            page_key = canonical_identifier(unquote(path))
            for mod in catalog_mods:
                if page_key and page_key in {
                    canonical_identifier(mod.canonical_id),
                    canonical_identifier(mod.mod_id),
                    canonical_identifier(mod.name),
                }:
                    return mod

    name_key = canonical_identifier(target_name)
    if name_key:
        for mod in catalog_mods:
            if name_key in {
                canonical_identifier(mod.name),
                canonical_identifier(mod.mod_id),
                canonical_identifier(mod.canonical_id),
            }:
                return mod
    return None
