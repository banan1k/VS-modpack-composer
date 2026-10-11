from types import SimpleNamespace

from app.services.catalog_requests import (
    catalog_request_key,
    find_catalog_mod,
    is_moddb_page_url,
    normalized_moddb_page_url,
)


def _mod(
    *,
    id=1,
    name="Fixed Paths",
    canonical_id="fixedpaths",
    mod_id="fixedpaths",
    mod_db_url="https://mods.vintagestory.at/fixedpaths",
    mod_db_asset_id=123,
):
    return SimpleNamespace(
        id=id,
        name=name,
        canonical_id=canonical_id,
        mod_id=mod_id,
        mod_db_url=mod_db_url,
        mod_db_asset_id=mod_db_asset_id,
    )


def test_moddb_page_url_allows_mod_pages_but_rejects_untrusted_or_non_page_urls():
    assert is_moddb_page_url("https://mods.vintagestory.at/fixedpaths")
    assert is_moddb_page_url("https://mods.vintagestory.at/show/mod/123")
    assert not is_moddb_page_url("https://example.com/fixedpaths")
    assert not is_moddb_page_url("https://mods.vintagestory.at/api/v2/mods/123")
    assert not is_moddb_page_url("javascript:alert(1)")


def test_normalized_url_ignores_scheme_query_and_trailing_slash():
    assert normalized_moddb_page_url("https://mods.vintagestory.at/fixedpaths/?ref=abc") == (
        "mods.vintagestory.at/fixedpaths"
    )
    assert catalog_request_key("https://mods.vintagestory.at/fixedpaths") == catalog_request_key(
        "http://www.mods.vintagestory.at/fixedpaths/?ref=abc"
    )


def test_find_catalog_mod_matches_by_public_asset_id_even_if_url_format_differs():
    mod = _mod()
    assert find_catalog_mod("Other Name", "https://mods.vintagestory.at/show/mod/123", None, [mod]) is mod


def test_find_catalog_mod_matches_by_slug_and_exact_name():
    mod = _mod()
    assert find_catalog_mod("not the same label", "https://mods.vintagestory.at/fixedpaths", None, [mod]) is mod
    assert find_catalog_mod("Fixed Paths", "https://mods.vintagestory.at/unresolved-page", None, [mod]) is mod


def test_find_catalog_mod_returns_none_for_target_not_present():
    assert find_catalog_mod("Unknown Mod", "https://mods.vintagestory.at/unknownmod", None, [_mod()]) is None
