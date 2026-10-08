from app.services.versioning import is_version_compatible, latest_game_version


def test_game_branch_compatibility_ignores_patch_number():
    assert is_version_compatible(["1.22.0 - 1.22.6"], "1.22.7")
    assert not is_version_compatible(["1.21.9"], "1.22.7")


def test_latest_game_version_uses_highest_version_in_range():
    assert latest_game_version(["1.22.0 - 1.22.6", "1.21.5"]) == "1.22.6"


def test_latest_game_version_handles_mixed_precision_versions():
    assert latest_game_version(["1.22", "1.22.7", "1.22.6.1"]) == "1.22.7"

def test_latest_game_version_handles_prerelease_suffixes_without_type_error():
    assert latest_game_version(["1.22.7-pre", "1.22.6", "1.22.7"]) == "1.22.7"


def test_latest_game_version_ignores_uncomparable_suffix_shapes():
    assert latest_game_version(["1.22", "1.22.7", "1.22.7-pre.1", "1.21.12"]) == "1.22.7"
