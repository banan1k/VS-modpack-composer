from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ModDBData:
    mod_db_id: int | None
    asset_id: int | None
    mod_id: str | None
    name: str
    description: str | None
    short_description: str | None
    image_url: str | None
    author: str | None
    mod_type: str | None
    mod_db_url: str
    releases: list[dict[str, Any]]
    source_updated_at: datetime | None
    raw_json: str
    notes: list[str] = field(default_factory=list)


class ModDBClient:
    def __init__(self, api_base: str, site_base: str, timeout: float = 30.0):
        self.api_base = api_base.rstrip("/")
        self.site_base = site_base.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._catalog_rows: list[dict[str, Any]] | None = None
        self._catalog_loaded_at = 0.0
        self._catalog_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        timeout=self.timeout,
                        follow_redirects=True,
                        headers={"User-Agent": "VS-Modpack-Builder/0.5"},
                    )
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_catalog_rows(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not force_refresh and self._catalog_rows is not None and now - self._catalog_loaded_at < 600:
            return self._catalog_rows

        async with self._catalog_lock:
            now = time.monotonic()
            if not force_refresh and self._catalog_rows is not None and now - self._catalog_loaded_at < 600:
                return self._catalog_rows
            client = await self._get_client()
            response = await client.get(f"{self.api_base}/mods")
            response.raise_for_status()
            rows = _extract_rows(response.json())
            if not rows:
                raise RuntimeError("Mod DB /api/mods вернул пустой каталог")
            self._catalog_rows = rows
            self._catalog_loaded_at = time.monotonic()
            logger.info("[MODDB] compact catalog loaded: %s rows", len(rows))
            return rows

    async def get_mod(
        self,
        identifier: str,
        source_url: str | None = None,
        catalog_rows: list[dict[str, Any]] | None = None,
    ) -> ModDBData:
        """Get Mod DB data without ever confusing assetid with API modid.

        `/show/mod/N` is an asset/page identifier. We first resolve it through /api/mods.
        We intentionally never call /api/mod/N directly when N came from /show/mod/N.
        """
        if catalog_rows is None:
            catalog_rows = await self.get_catalog_rows()

        source_num, source_alias = _source_identity(source_url)
        summary = _pick_summary(
            catalog_rows,
            name=source_alias or identifier,
            mod_id=source_alias,
            mod_db_id=None,
            source_url=source_url,
        )

        resolved_api_identifier = None
        if summary:
            resolved_api_identifier = _first_text(summary.get("modid"), summary.get("modId"))

        # For alias pages we can safely use the alias as the API identifier if catalog lookup
        # did not produce a row. For /show/mod/N, do NOT fall back to N.
        if not resolved_api_identifier and source_alias:
            resolved_api_identifier = source_alias

        payload: dict[str, Any] = {}
        notes: list[str] = []
        if resolved_api_identifier:
            client = await self._get_client()
            try:
                detail_response = await client.get(f"{self.api_base}/mod/{resolved_api_identifier}")
                detail_response.raise_for_status()
                raw_payload = detail_response.json()
                if isinstance(raw_payload, dict):
                    payload = raw_payload
                logger.debug("[MODDB] detail OK source=%s api_id=%s", source_url, resolved_api_identifier)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"API: {type(exc).__name__}: {exc}")
                logger.warning(
                    "[MODDB] detail failed source=%s api_id=%s: %s",
                    source_url,
                    resolved_api_identifier,
                    exc,
                )
        else:
            notes.append("API: internal modid could not be resolved from /api/mods")

        root = payload.get("mod", payload) if isinstance(payload, dict) else {}
        if not isinstance(root, dict):
            root = {}
        releases = root.get("releases") or []
        if not isinstance(releases, list):
            releases = []

        mod_db_id = _int_or_none(root.get("modid") or root.get("modId"))
        if mod_db_id is None and summary:
            mod_db_id = _int_or_none(summary.get("modid") or summary.get("modId"))

        asset_id = _int_or_none(root.get("assetid") or root.get("assetId"))
        if asset_id is None and summary:
            asset_id = _int_or_none(summary.get("assetid") or summary.get("assetId"))
        if asset_id is None:
            asset_id = source_num

        mod_id = _first_text(
            root.get("modidstr"),
            root.get("urlalias"),
            root.get("mod_id"),
            root.get("identifier"),
            summary.get("modidstr") if summary else None,
            summary.get("urlalias") if summary else None,
            summary.get("mod_id") if summary else None,
            summary.get("identifier") if summary else None,
        )
        if not mod_id:
            for release in releases:
                if isinstance(release, dict):
                    mod_id = _first_text(release.get("modidstr"), release.get("identifier"))
                    if mod_id:
                        break
        if not mod_id and source_alias:
            mod_id = source_alias

        name = _first_text(
            root.get("name"),
            root.get("title"),
            summary.get("name") if summary else None,
            summary.get("title") if summary else None,
            source_alias,
            identifier if not str(identifier).isdigit() else None,
            f"Mod {source_num}" if source_num is not None else None,
        ) or "Неизвестный мод"

        description = _clean_htmlish(root.get("text") or root.get("description"))
        image_url = _absolute_url(
            root.get("logofilename") or root.get("logofile") or root.get("logofiledb"),
            self.site_base,
        )
        short_description = self._summary_text(summary)

        if summary:
            summary_image = _absolute_url(
                summary.get("logofilename") or summary.get("logofile") or summary.get("logofiledb"),
                self.site_base,
            )
            image_url = image_url or summary_image
            short_description = short_description or self._summary_text(summary)

        page_url = source_url if source_url and _is_mod_page_url(source_url) else self._page_from_identity(asset_id, mod_id)
        raw_json = json.dumps(payload or {"summary": summary or {}}, ensure_ascii=False, sort_keys=True)
        return ModDBData(
            mod_db_id=mod_db_id,
            asset_id=asset_id,
            mod_id=mod_id,
            name=name,
            description=description,
            short_description=short_description,
            image_url=image_url,
            author=_first_text(root.get("author")),
            mod_type=_first_text(root.get("type")),
            mod_db_url=page_url,
            releases=[r for r in releases if isinstance(r, dict)],
            source_updated_at=_parse_dt(root.get("lastmodified")),
            raw_json=raw_json,
            notes=notes,
        )

    async def get_compact_summary(
        self,
        name: str,
        mod_id: str | None,
        mod_db_id: int | None,
        source_url: str | None = None,
        catalog_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        rows = catalog_rows if catalog_rows is not None else await self.get_catalog_rows()
        return _pick_summary(rows, name, mod_id, mod_db_id, source_url=source_url)

    async def get_short_description(
        self, name: str, mod_id: str | None, mod_db_id: int | None
    ) -> str | None:
        summary = await self.get_compact_summary(name, mod_id, mod_db_id)
        return self._summary_text(summary)

    @staticmethod
    def _summary_text(summary: dict[str, Any] | None) -> str | None:
        if not summary:
            return None
        value = (
            summary.get("shortdescription")
            or summary.get("shortDescription")
            or summary.get("summary")
            or summary.get("description")
            or summary.get("text")
        )
        cleaned = _clean_htmlish(value)
        return _compact_card_text(cleaned) if cleaned else None

    async def get_page(self, url: str) -> str:
        client = await self._get_client()
        response = await client.get(url)
        response.raise_for_status()
        return response.text

    @staticmethod
    def enrich_from_page_html(data: ModDBData, html: str) -> ModDBData:
        """Use the exact Mod DB page as the authoritative identity/metadata source.

        In particular:
        - never use the first H1 as the title (it may be a Disclaimer/modal heading);
        - prefer OG title/description/image from this exact page;
        - extract the in-game mod identifier only from the release table;
        - preserve the Discord-supplied page URL unchanged.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")

        page_title = _meta_content(soup, {"property": "og:title"}) or _meta_content(
            soup, {"name": "twitter:title"}
        )
        if not page_title and soup.title:
            page_title = " ".join(soup.title.stripped_strings)
        page_title = _clean_page_title(page_title)
        if page_title:
            data.name = page_title

        source_num, source_alias = _source_identity(data.mod_db_url)
        if source_num is not None:
            data.asset_id = source_num

        for attrs in ({"property": "og:url"}, {"name": "twitter:url"}):
            canonical = _meta_content(soup, attrs)
            if canonical:
                can_num, can_alias = _source_identity(canonical)
                if can_num is not None:
                    data.asset_id = can_num
                if not source_alias and can_alias and not data.mod_id:
                    data.mod_id = can_alias
                break
        for link in soup.find_all("link"):
            rel_values = link.get("rel") or []
            if any(str(v).casefold() == "canonical" for v in rel_values) and link.get("href"):
                can_num, can_alias = _source_identity(str(link["href"]))
                if can_num is not None:
                    data.asset_id = can_num
                if not source_alias and can_alias and not data.mod_id:
                    data.mod_id = can_alias
                break

        identifier = _release_table_identifier(soup)
        if identifier:
            data.mod_id = identifier

        page_short = _meta_content(soup, {"property": "og:description"}) or _meta_content(
            soup, {"name": "description"}
        )
        if page_short:
            data.short_description = _compact_card_text(page_short)

        page_image = _meta_content(soup, {"property": "og:image"}) or _meta_content(
            soup, {"name": "twitter:image"}
        )
        if page_image:
            # The exact page's image wins over the compact API snapshot. This prevents one
            # wrong API-row mapping from leaking another mod's cover into this card.
            data.image_url = _absolute_url(page_image, "https://mods.vintagestory.at")

        if not data.description:
            description_text = _description_text_from_page(soup)
            if description_text:
                data.description = description_text

        if not data.mod_id:
            data.mod_id = source_alias
        if not data.mod_id and data.asset_id is not None:
            # Never display an empty ID for a successfully fetched page. This is an explicit
            # fallback, not a claim that the asset number is the in-game modinfo identifier.
            data.mod_id = str(data.asset_id)

        if not data.name:
            data.name = f"Mod {data.asset_id}" if data.asset_id is not None else "Неизвестный мод"
        return data

    async def get_install_information(self, specs: list[str], game_version: str | None) -> dict[str, Any]:
        if not specs:
            return {}
        client = await self._get_client()
        params: dict[str, str] = {"ids": ",".join(specs)}
        if game_version:
            params["gv"] = game_version
        response = await client.get(f"{self.api_base}/v2/mods/install-information", params=params)
        response.raise_for_status()
        return response.json()

    def _page_from_identity(self, asset_id: int | None, mod_id: str | None) -> str:
        if asset_id is not None:
            return f"{self.site_base}/show/mod/{asset_id}"
        if mod_id and not mod_id.isdigit():
            return f"{self.site_base}/{mod_id}"
        return self.site_base


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("mods", "data", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return [v for v in payload.values() if isinstance(v, dict)]


def _pick_summary(
    rows: list[dict[str, Any]],
    name: str,
    mod_id: str | None,
    mod_db_id: int | None,
    source_url: str | None = None,
):
    target_name = _norm(name)
    target_id = _norm(mod_id or "")
    source_num, source_alias = _source_identity(source_url)

    if source_num is not None:
        for row in rows:
            if _int_or_none(row.get("assetid") or row.get("assetId")) == source_num:
                return row

    if source_alias:
        for row in rows:
            row_id = _row_mod_identifier(row)
            if row_id == _norm(source_alias):
                return row

    if mod_db_id is not None:
        for row in rows:
            if _int_or_none(row.get("modid") or row.get("modId")) == mod_db_id:
                return row

    if target_id:
        for row in rows:
            if _row_mod_identifier(row) == target_id:
                return row

    if target_name:
        for row in rows:
            row_name = _norm(row.get("name") or row.get("title") or "")
            if row_name and row_name == target_name:
                return row
    return None


def _row_mod_identifier(row: dict[str, Any]) -> str:
    return _norm(
        row.get("modidstr")
        or row.get("urlalias")
        or row.get("mod_id")
        or row.get("identifier")
        or ""
    )


def _meta_content(soup, attrs: dict[str, str]) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    if tag and tag.get("content"):
        value = " ".join(str(tag["content"]).split()).strip()
        return value or None
    return None


def _clean_page_title(value: str | None) -> str | None:
    if not value:
        return None
    value = " ".join(value.split()).strip()
    value = re.sub(r"\s*[|–—-]\s*Vintage Story Mod DB\s*$", "", value, flags=re.I)
    value = re.sub(r"\s+[-|]\s+Mods\s*$", "", value, flags=re.I)
    if value.casefold() in {"disclaimer", "mods", "mod info", "description", "files"}:
        return None
    return value or None


def _description_text_from_page(soup) -> str | None:
    root = _find_description_root(soup)
    if root is None:
        return None
    text_parts = []
    for node in root.find_all(["p", "div", "blockquote"], recursive=True):
        text = " ".join(node.stripped_strings).strip()
        if text and len(text) > 20:
            text_parts.append(text)
    if not text_parts:
        text = " ".join(root.stripped_strings).strip()
        return text[:5000] if text else None
    # Keep only the first few unique blocks; full description is a fallback only.
    unique = list(dict.fromkeys(text_parts))
    return "\n\n".join(unique[:20])[:5000]


def _find_description_root(soup):
    candidates = []
    for tag in soup.find_all(True):
        marker = " ".join(str(v) for v in [tag.get("id"), *(tag.get("class") or [])] if v).casefold()
        if "comment" in marker or "release" in marker or "changelog" in marker:
            continue
        if "tab-description" in marker or re.search(r"(?:^|[-_ ])description(?:$|[-_ ])", marker):
            if tag.name not in {"a", "button", "nav", "form"}:
                candidates.append(tag)
    if candidates:
        return max(candidates, key=lambda t: len(list(t.stripped_strings)))
    return soup.body or soup


def _compact_card_text(value: str, limit: int = 240) -> str:
    value = " ".join(str(value).split())
    if not value:
        return value
    sentence = re.split(r"(?<=[.!?])\s+", value, maxsplit=1)[0].strip()
    if 30 <= len(sentence) <= limit:
        return sentence
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _release_table_identifier(soup) -> str | None:
    best: tuple[tuple[int, str], str] | None = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        headers = [" ".join(cell.stripped_strings).casefold() for cell in header_cells]
        if not any("mod identifier" in h for h in headers):
            continue
        identifier_index = next(i for i, h in enumerate(headers) if "mod identifier" in h)
        for row in rows[1:]:
            cells = [" ".join(c.stripped_strings).strip() for c in row.find_all(["td", "th"])]
            if len(cells) <= identifier_index:
                continue
            candidate = cells[identifier_index].strip()
            if not candidate or candidate.casefold() in {"mod identifier", "-"}:
                continue
            version_text = cells[0] if cells else ""
            version_key = _version_key(version_text)
            if best is None or version_key > best[0]:
                best = (version_key, candidate)
    return best[1] if best else None


def _version_key(value: str):
    raw_nums = [int(x) for x in re.findall(r"\d+", value)[:4]]
    nums = tuple((raw_nums + [0, 0, 0, 0])[:4])
    stable = 1 if not re.search(r"(?:pre|rc|alpha|beta|dev)", value, re.I) else 0
    return nums + (stable, value.casefold())


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _absolute_url(value: Any, base: str) -> str | None:
    if not value:
        return None
    return urljoin(base.rstrip("/") + "/", str(value))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean_htmlish(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    from bs4 import BeautifulSoup

    return " ".join(BeautifulSoup(value, "lxml").stripped_strings)


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


def _source_identity(source_url: str | None) -> tuple[int | None, str | None]:
    if not source_url:
        return None, None
    parsed = urlparse(source_url)
    if parsed.netloc.casefold() not in {"mods.vintagestory.at", "www.mods.vintagestory.at"}:
        return None, None
    path = parsed.path.rstrip("/")
    m = re.fullmatch(r"/show/mod/(\d+)", path, re.I)
    if m:
        return int(m.group(1)), None
    if re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9._-]{1,119}", path):
        return None, path[1:]
    return None, None
