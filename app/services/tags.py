"""Helpers for stable Discord forum-tag storage and display."""

from __future__ import annotations

from collections.abc import Iterable
import json


def normalize_tag_names(values: Iterable[object] | None) -> list[str]:
    """Return unique, trimmed tag names in deterministic case-insensitive order."""
    unique: dict[str, str] = {}
    for value in values or []:
        raw = getattr(value, "name", value)
        name = str(raw or "").strip()
        if not name:
            continue
        unique.setdefault(name.casefold(), name)
    return sorted(unique.values(), key=lambda value: (value.casefold(), value))


def tag_names_json(values: Iterable[object] | None) -> str:
    """Serialize normalized forum-tag names for storage on catalog and addon rows."""
    return json.dumps(normalize_tag_names(values), ensure_ascii=False)


def preserved_forum_tag_ids(source_tags: Iterable[object], target_tags: Iterable[object]) -> list[int | None]:
    """Plan target-local IDs for mirrored forum tags.

    Same-name tags keep their target channel IDs. For a rename, reuse the old ID at the same
    position only when that old name was removed from the source list; this preserves applied
    thread tags for the common in-place-rename workflow without confusing reorder operations.
    """
    source = list(source_tags or [])
    target = list(target_tags or [])
    source_names = [str(getattr(tag, "name", tag) or "").strip().casefold() for tag in source]
    source_name_set = set(source_names)
    target_by_name = {str(getattr(tag, "name", "") or "").strip().casefold(): tag for tag in target}
    used_ids: set[int] = set()
    planned: list[int | None] = []

    for index, tag in enumerate(source):
        name = str(getattr(tag, "name", tag) or "").strip().casefold()
        match = target_by_name.get(name) if name else None
        if match is not None and int(getattr(match, "id", 0) or 0) in used_ids:
            match = None
        if match is None and len(source) == len(target) and index < len(target):
            candidate = target[index]
            candidate_id = int(getattr(candidate, "id", 0) or 0)
            candidate_name = str(getattr(candidate, "name", "") or "").strip().casefold()
            if candidate_id and candidate_id not in used_ids and candidate_name not in source_name_set:
                match = candidate
        tag_id = int(getattr(match, "id", 0) or 0) if match is not None else 0
        if not tag_id:
            planned.append(None)
        else:
            used_ids.add(tag_id)
            planned.append(tag_id)
    return planned


def tag_lists_match(stored: Iterable[object] | None, current: Iterable[object] | None) -> bool:
    """Compare persisted and current tag names without depending on JSON order/case."""
    stored_normalized = [tag.casefold() for tag in normalize_tag_names(stored)]
    current_normalized = [tag.casefold() for tag in normalize_tag_names(current)]
    return stored_normalized == current_normalized


def thread_tag_names(thread, forum=None) -> list[str]:
    """Resolve the names of tags assigned to a Discord forum thread.

    Prefer discord.py's resolved `applied_tags`. If that list is empty, fall back to the
    thread's raw applied-tag IDs and the parent forum's available tag definitions.
    """
    tags = normalize_tag_names(getattr(thread, "applied_tags", []) or [])
    if tags:
        return tags

    raw_ids = getattr(thread, "_applied_tags", None)
    if raw_ids is None:
        raw_ids = getattr(thread, "applied_tag_ids", None)
    try:
        ids = {int(value) for value in (raw_ids or [])}
    except (TypeError, ValueError):
        ids = set()
    if not ids or forum is None:
        return []

    available = getattr(forum, "available_tags", []) or []
    resolved = [tag for tag in available if int(getattr(tag, "id", 0) or 0) in ids]
    return normalize_tag_names(resolved)
