from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

KEYWORDS: dict[str, tuple[str, ...]] = {
    "required_by": ("required by",),
    "incompatible": ("incompatible with", "conflicts with", "conflict", "incompatible"),
    "optional_dependency": (
        "optional dependency",
        "optional dependencies",
        "recommended",
        "recommend",
    ),
    "compatible": (
        "compatible with",
        "compatibility",
        "compatible",
        "works with",
        "works alongside",
        "works together with",
        "supported mods",
    ),
    "dependency": (
        "requires mods",
        "requires mod",
        "requires",
        "required",
        "require",
        "dependencies",
        "dependency",
        "depends upon",
        "depends on",
        "depend on",
        "mod depends upon",
        "mod depends on",
        "needs",
        "needed",
    ),
    "addon": ("addon", "add-on"),
}

OPTIONAL_HINTS = (
    "optional",
    "recommend",
    "recommended",
    "without it",
    "if installed",
    "only active with",
    "only works with",
)

DOWNLOAD_MARKERS = (
    "recommended download",
    "latest release",
    "1-click install",
)


@dataclass
class RelationCandidate:
    relation_type: str
    url: str | None
    target_name: str | None
    raw_phrase: str
    evidence: str
    confidence: float


def extract_api_relationships(payload: dict) -> list[RelationCandidate]:
    out: list[RelationCandidate] = []
    root = payload.get("mod", payload) if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return out
    field_map = {
        "dependencies": "dependency",
        "dependency": "dependency",
        "requires": "dependency",
        "required": "dependency",
        "optionalDependencies": "optional_dependency",
        "optional_dependencies": "optional_dependency",
        "recommended": "optional_dependency",
        "compatible": "compatible",
        "compatibilities": "compatible",
        "incompatible": "incompatible",
        "conflicts": "incompatible",
        "addons": "addon",
        "add-ons": "addon",
    }
    for key, kind in field_map.items():
        value = root.get(key)
        values = value if isinstance(value, list) else [value] if value else []
        for item in values:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title") or item.get("modid") or item.get("mod_id")
                url = item.get("url") or item.get("mod_db_url")
            else:
                name, url = str(item), None
            if name or url:
                out.append(RelationCandidate(kind, url, name, key, str(item), 0.99 if url else 0.90))
    return out


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace("_", " ").replace("-", " ")
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def extract_relationships(html: str, known_mods: list[dict]) -> list[RelationCandidate]:
    """Extract relations from the mod's description, not the page as a whole.

    The parser deliberately uses direct Mod DB links as the primary target signal. Text-only
    detection is allowed only when the nearby description block names a mod known to the local
    Discord-derived catalog. Comments, release/download UI and changelogs are hard boundaries.
    """
    soup = BeautifulSoup(html, "lxml")
    title = _page_title(soup)
    description_root, title_heading = _find_description_region(soup, title)
    if description_root is None:
        return []

    _remove_non_description_noise(description_root)

    known_by_name: dict[str, dict] = {}
    known_by_id: dict[str, dict] = {}
    known_by_url: dict[str, dict] = {}
    for mod in known_mods:
        if mod.get("name"):
            known_by_name[normalize_name(str(mod["name"]))] = mod
        if mod.get("mod_id"):
            known_by_id[normalize_name(str(mod["mod_id"]))] = mod
        urls = [mod.get("mod_db_url"), *(mod.get("mod_db_urls") or [])]
        for raw_url in urls:
            if raw_url:
                known_by_url[_canonical_url(str(raw_url))] = mod

    candidates: list[RelationCandidate] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    active_section: str | None = None
    changelog_level: int | None = None
    started = title_heading is None

    nodes = description_root.find_all(True)
    for node in nodes:
        if title_heading is not None:
            if node is title_heading:
                started = True
                continue
            if not started:
                continue

        if _is_hard_boundary(node):
            break
        if node.name in {"script", "style", "noscript", "template", "nav", "footer", "form", "table", "pre", "code"}:
            continue
        if _is_commentish(node):
            continue

        if re.fullmatch(r"h[2-6]", node.name or ""):
            heading = " ".join(node.stripped_strings).strip()
            level = int(node.name[1])
            if _is_comments_heading(heading):
                break
            if changelog_level is not None and level <= changelog_level:
                changelog_level = None
            if _looks_like_changelog_heading(heading) or normalize_name(heading) in {"changelog", "changes", "version history"}:
                changelog_level = level
                active_section = None
                continue
            if changelog_level is not None:
                continue
            active_section = _section_relation(heading)
            continue

        if changelog_level is not None:
            continue

        if node.name not in {"p", "li", "dt", "dd", "blockquote"}:
            continue
        if any(isinstance(c, Tag) and c.name in {"p", "li", "dt", "dd", "blockquote"} for c in node.children):
            continue

        text = " ".join(node.stripped_strings).strip()
        if not text or _looks_like_download_block(text) or _looks_like_release_text(text):
            continue

        links = []
        for anchor in node.find_all("a", href=True):
            href = urljoin("https://mods.vintagestory.at", str(anchor.get("href")))
            if _is_mod_page_url(href):
                links.append((anchor, _canonical_url(href)))
        links = _dedupe_links(links)

        matches = _find_keyword_matches(text)
        relation_kind = _classify_relation(matches, active_section, text.casefold())

        # A bare link under a specifically recognized relation heading is safe to classify from
        # that heading. A bare link elsewhere is merely an optional suggestion with <90% confidence.
        if relation_kind is None and links and active_section is not None:
            relation_kind = active_section
        elif relation_kind is None and links:
            relation_kind = "optional_dependency"

        if relation_kind is None:
            continue

        if links:
            for anchor, url in links:
                target_name = anchor.get_text(" ", strip=True) or _mod_name_from_url(url)
                known = _match_known(target_name, url, known_by_name, known_by_id, known_by_url)
                naked_optional = not matches and active_section is None
                if relation_kind == "optional_dependency" and naked_optional:
                    confidence = 0.89
                elif known:
                    confidence = 0.97 if matches or active_section in {"compatible", "incompatible", "optional_dependency"} else 0.92
                else:
                    confidence = 0.92 if matches or active_section in {"compatible", "incompatible", "optional_dependency"} else 0.89
                key = (relation_kind, url, normalize_name(target_name or ""))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    RelationCandidate(
                        relation_kind,
                        url,
                        known.get("name") if known else target_name,
                        _relation_phrase(matches, active_section) or relation_kind,
                        text[:1000],
                        confidence,
                    )
                )
            continue

        # No link: require an exact/local catalog name in the same description block. The old
        # parser tried to treat all words after `needs`/`dependency` as a target; that is what
        # created false positives such as "a server restart to apply" and comment text.
        target_name = _match_known_name_near_keyword(text, matches, known_by_name)
        if not target_name:
            target_name = _match_known_name_near_keyword(text, matches, known_by_id)
        if not target_name:
            continue
        known = _match_known(target_name, None, known_by_name, known_by_id, known_by_url)
        display_name = known.get("name") if known else target_name
        key = (relation_kind, None, normalize_name(display_name))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            RelationCandidate(
                relation_kind,
                None,
                display_name,
                _relation_phrase(matches, active_section) or relation_kind,
                text[:1000],
                0.86 if known else 0.64,
            )
        )

    return candidates


def _page_title(soup: BeautifulSoup) -> str | None:
    tag = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "twitter:title"})
    value = tag.get("content") if tag else None
    if not value and soup.title:
        value = " ".join(soup.title.stripped_strings)
    if not value:
        return None
    value = " ".join(str(value).split()).strip()
    value = re.sub(r"\s*[|–—-]\s*Vintage Story Mod DB\s*$", "", value, flags=re.I)
    if normalize_name(value) in {"disclaimer", "mods", "description", "files", "mod info"}:
        return None
    return value or None


def _find_description_region(soup: BeautifulSoup, title: str | None):
    # Strong selectors first: the actual description tab/container in Mod DB skins.
    strong = []
    for tag in soup.find_all(True):
        marker = " ".join(str(v) for v in [tag.get("id"), *(tag.get("class") or [])] if v).casefold()
        if "comment" in marker or "changelog" in marker or "release" in marker:
            continue
        if "tab-description" in marker:
            strong.append(tag)
    if strong:
        return max(strong, key=lambda t: len(list(t.stripped_strings))), None

    title_heading = None
    if title:
        wanted = normalize_name(title)
        for heading in soup.find_all(re.compile(r"^h[1-6]$")):
            if normalize_name(" ".join(heading.stripped_strings)) == wanted:
                title_heading = heading
                break

    description_like = []
    for tag in soup.find_all(True):
        marker = " ".join(str(v) for v in [tag.get("id"), *(tag.get("class") or [])] if v).casefold()
        if "description" in marker and "comment" not in marker and tag.name not in {"a", "button", "nav", "form"}:
            description_like.append(tag)
    if description_like:
        root = max(description_like, key=lambda t: len(list(t.stripped_strings)))
        if title_heading is not None and not root.find(lambda x: x is title_heading):
            title_heading = None
        return root, title_heading

    # Body fallback, but only after identifying the real title. We never start relation parsing
    # from the first H1 because Mod DB can put a Disclaimer H1/modal before the mod title.
    return soup.body or soup, title_heading


def _remove_non_description_noise(root: Tag) -> None:
    for tag in list(root.find_all(["script", "style", "noscript", "template", "nav", "footer", "form", "pre", "code"])):
        tag.decompose()
    for tag in list(root.find_all(True)):
        if _is_commentish(tag):
            tag.decompose()


def _is_hard_boundary(tag: Tag) -> bool:
    if tag.name == "table" and _looks_like_release_table(tag):
        return True
    if re.fullmatch(r"h[1-6]", tag.name or "") and _is_comments_heading(" ".join(tag.stripped_strings)):
        return True
    marker = " ".join(str(v) for v in [tag.get("id"), *(tag.get("class") or [])] if v).casefold()
    return bool(marker and re.search(r"(?:^|[-_ ])(?:release|releases|changelog|comments|comment-list|replies)(?:$|[-_ ])", marker))


def _is_commentish(tag: Tag) -> bool:
    marker = " ".join(
        str(v)
        for v in [tag.get("id"), *(tag.get("class") or []), tag.get("data-section"), tag.get("data-testid")]
        if v
    )
    return bool(marker and re.search(r"(?:comment|comment-list|repl(?:y|ies)|pagination)", marker, re.I))


def _is_comments_heading(text: str) -> bool:
    return bool(re.match(r"^(?:\d+\s+)?comments?\b", text.strip(), re.I))


def _looks_like_release_table(table: Tag) -> bool:
    text = " ".join(table.stripped_strings).casefold()
    return "mod version" in text and "mod identifier" in text and ("download" in text or "for game version" in text)


def _looks_like_download_block(text: str) -> bool:
    lower = text.casefold()
    if any(marker in lower for marker in DOWNLOAD_MARKERS):
        return True
    return bool(re.search(r"download\s*\(\s*for\s+vintage\s+story\b", lower))


def _looks_like_release_text(text: str) -> bool:
    lower = text.casefold()
    return bool("mod version" in lower and "mod identifier" in lower and "for game version" in lower)


def _looks_like_changelog_heading(text: str) -> bool:
    compact = " ".join(text.split())
    return bool(
        re.match(r"^v?\d+(?:\.\d+){1,3}\b", compact, re.I)
        or re.match(r"^\d+(?:\.\d+){1,3}\s+-\s+\d{4}-\d{2}-\d{2}", compact)
    )


def _section_relation(heading: str) -> str | None:
    h = normalize_name(heading)
    if any(x in h for x in ("incompatible", "conflicts", "conflict")):
        return "incompatible"
    if any(x in h for x in ("compatible", "compatibility", "supported mods", "supported mod", "integrations", "integration")):
        return "compatible"
    if any(x in h for x in ("optional dependencies", "optional dependency", "recommended", "recommendations", "recommended mods", "other recommended mods")):
        return "optional_dependency"
    if any(x in h for x in ("dependencies", "requirements", "requires")):
        return "dependency"
    if any(x in h for x in ("addons", "add ons", "add ons", "add on", "add-on")):
        return "addon"
    return None


def _classify_relation(matches: list[tuple[str, str]], active_section: str | None, lower: str) -> str | None:
    explicit = [kind for kind, _ in matches]
    if "incompatible" in explicit:
        return "incompatible"
    if "required_by" in explicit:
        return "required_by"
    if "optional_dependency" in explicit:
        return "optional_dependency"
    if "dependency" in explicit:
        if any(hint in lower for hint in OPTIONAL_HINTS):
            return "optional_dependency"
        return "dependency"
    if "compatible" in explicit:
        return "compatible"
    if "addon" in explicit:
        return "addon"
    return active_section


def _find_keyword_matches(text: str) -> list[tuple[str, str]]:
    matches: list[tuple[int, int, str, str]] = []
    for kind, phrases in KEYWORDS.items():
        for phrase in sorted(phrases, key=len, reverse=True):
            for match in re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, flags=re.I):
                if kind == "dependency" and phrase.casefold() == "required":
                    if re.match(r"\s+by\b", text[match.end():], re.I):
                        continue
                matches.append((match.start(), match.end(), kind, phrase))
    matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    accepted: list[tuple[int, int, str, str]] = []
    for candidate in matches:
        start, end, *_ = candidate
        if any(start < end2 and end > start2 for start2, end2, *_ in accepted):
            continue
        accepted.append(candidate)
    accepted.sort(key=lambda x: x[0])
    return [(kind, phrase) for _start, _end, kind, phrase in accepted]


def _relation_phrase(matches: list[tuple[str, str]], active_section: str | None) -> str | None:
    return max((phrase for _kind, phrase in matches), key=len, default=active_section)


def _match_known_name_near_keyword(text: str, matches: list[tuple[str, str]], index: dict[str, dict]) -> str | None:
    if not matches:
        return None
    # Keep the window small. This avoids selecting arbitrary names that happen to appear later
    # in a long paragraph about a different topic.
    norm_full = normalize_name(text)
    if len(norm_full) > 500:
        windows = []
        for phrase in matches:
            pos = normalize_name(phrase[1])
            idx = norm_full.find(pos)
            if idx >= 0:
                windows.append(norm_full[max(0, idx - 140): idx + len(pos) + 180])
    else:
        windows = [norm_full]
    hits: list[tuple[int, str]] = []
    for window in windows:
        padded = f" {window} "
        for key, row in index.items():
            if len(key) < 3:
                continue
            if f" {key} " in padded:
                hits.append((len(key), row.get("name") or row.get("mod_id")))
    return max(hits, default=(0, None))[1]


def _match_known(target_name: str | None, url: str | None, by_name: dict, by_id: dict, by_url: dict):
    if url:
        hit = by_url.get(_canonical_url(url))
        if hit:
            return hit
    if target_name:
        key = normalize_name(target_name)
        return by_name.get(key) or by_id.get(key) or {}
    return {}


def _dedupe_links(links: list[tuple[Tag, str]]) -> list[tuple[Tag, str]]:
    seen = set()
    out = []
    for anchor, url in links:
        canonical = _canonical_url(url)
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append((anchor, canonical))
    return out


def _is_mod_page_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.casefold() not in {"mods.vintagestory.at", "www.mods.vintagestory.at"}:
        return False
    path = parsed.path.rstrip("/")
    if re.fullmatch(r"/show/mod/\d+", path, re.I):
        return True
    if path.startswith(("/api/", "/download/", "/show/user/", "/list/", "/versionchecker")):
        return False
    return bool(re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9._-]{1,119}", path))


def _canonical_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.scheme.lower() + "://" + parsed.netloc.lower() + parsed.path.rstrip("/")


def _mod_name_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1] if path else url
