from dataclasses import dataclass

from app.services.tags import normalize_tag_names


@dataclass
class ForumTagLike:
    name: str


def test_normalize_tag_names_sorts_and_deduplicates_case_insensitively():
    values = [
        ForumTagLike("Nature"),
        ForumTagLike(" survival "),
        ForumTagLike("nature"),
        ForumTagLike("Crafting"),
        "",
        None,
    ]

    assert normalize_tag_names(values) == ["Crafting", "Nature", "survival"]


def test_normalize_tag_names_handles_missing_values():
    assert normalize_tag_names(None) == []
    assert normalize_tag_names(["  ", None, "QoL"]) == ["QoL"]


def test_preserved_forum_tag_ids_keep_same_names_and_remap_in_place_renames():
    from app.services.tags import preserved_forum_tag_ids

    source = [ForumTagLike("Survival"), ForumTagLike("Crafting"), ForumTagLike("Building")]
    target = [
        type("TargetTag", (), {"name": "Survival", "id": 101})(),
        type("TargetTag", (), {"name": "Old Crafting", "id": 102})(),
        type("TargetTag", (), {"name": "Building", "id": 103})(),
    ]

    assert preserved_forum_tag_ids(source, target) == [101, 102, 103]


def test_preserved_forum_tag_ids_do_not_reuse_deleted_tag_id_for_new_tag_when_old_name_still_exists():
    from app.services.tags import preserved_forum_tag_ids

    source = [ForumTagLike("New tag"), ForumTagLike("Building")]
    target = [
        type("TargetTag", (), {"name": "Building", "id": 101})(),
    ]

    assert preserved_forum_tag_ids(source, target) == [None, 101]


def test_thread_tag_names_falls_back_to_parent_tag_ids():
    from app.services.tags import thread_tag_names

    applied = type("Thread", (), {"applied_tags": [], "_applied_tags": [11, 12]})()
    forum = type("Forum", (), {"available_tags": [
        type("Tag", (), {"name": "Nature", "id": 11})(),
        type("Tag", (), {"name": "Survival", "id": 12})(),
        type("Tag", (), {"name": "Unused", "id": 13})(),
    ]})()

    assert thread_tag_names(applied, forum) == ["Nature", "Survival"]


def test_thread_tag_names_uses_resolved_applied_tags_first():
    from app.services.tags import thread_tag_names

    applied = type("Thread", (), {"applied_tags": [ForumTagLike("Building")]})()
    forum = type("Forum", (), {"available_tags": []})()

    assert thread_tag_names(applied, forum) == ["Building"]


def test_tag_lists_match_detects_stale_persisted_tags_but_ignores_order_and_case():
    from app.services.tags import tag_lists_match

    assert tag_lists_match(["survival", "Nature"], ["Nature", "Survival"])
    assert not tag_lists_match([], ["Survival"])
