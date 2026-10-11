from pathlib import Path

from app.services.identity import canonical_identifier, is_core_mod_identifier, is_library_dependency
from app.services.modinfo import dependencies_from_modinfo
import ast
import re
from urllib.parse import urlparse


def test_core_vintage_story_components_are_not_third_party_dependencies():
    assert is_core_mod_identifier("game")
    assert is_core_mod_identifier("survival")
    assert is_core_mod_identifier("creative")
    assert dependencies_from_modinfo({
        "dependencies": {
            "game": "1.22.0",
            "survival": "1.22.0",
            "creative": "1.22.0",
            "exlib": "0.8.4",
            "ppex": "0.7.1",
        }
    }) == {"exlib": "0.8.4", "ppex": "0.7.1"}


def test_library_classifier_is_positive_and_conservative():
    assert is_library_dependency("imgui")
    assert is_library_dependency("libgui")
    assert is_library_dependency("musicvolumebalancer")
    assert is_library_dependency("exlib", name="Expanded Library", mod_type="code")
    assert is_library_dependency("shearlib", name="ShearLib", mod_type="code")
    assert not is_library_dependency("ancientarmory", name="Ancient Armory", mod_type="code")
    assert not is_library_dependency("apiary", name="Apiary", description="Adds bees and honey.", mod_type="code")
    assert not is_library_dependency("survival", name="Vanilla Survival Mod")


def test_canonical_id_matches_discord_title_to_modinfo_id():
    assert canonical_identifier("Ancient Armory") == canonical_identifier("ancientarmory")
    assert canonical_identifier("A Tailor's Delight") == canonical_identifier("atailorsdelight")


def test_cache_filename_strips_legacy_hash_prefix():
    source = Path("app/services/cache.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CacheManager")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_safe_filename")
    namespace = {"re": re, "urlparse": urlparse, "Path": Path}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<cache_filename>", "exec"), namespace)
    safe = namespace["_safe_filename"]
    assert safe("7ae190d4301b57ab-realisticwater_0.4.0.zip") == "realisticwater_0.4.0.zip"
    assert safe("realisticwater_0.4.0.zip") == "realisticwater_0.4.0.zip"
