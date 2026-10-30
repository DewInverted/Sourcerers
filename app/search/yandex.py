"""Yandex Images reverse search (no API key).

Two steps: upload the frame to get a ``cbir_id``, then read the results page
where the "sites containing this image" live in an ``initialState`` JSON blob
embedded in a ``data-state`` attribute.  Yandex is often more generous than
Lens with re-uploads and TikTok/IG mirrors, which makes it a good second
opinion.  There are two independent upload paths in case one is retired.
"""

from __future__ import annotations

import html as html_mod
import json
import re
from typing import Any, Iterable

import httpx

from .base import (
    Provider,
    ProviderOutput,
    RawResult,
    canonicalize,
    clean_url,
    is_interesting,
    urls_in_html,
)

UPLOAD_URL = "https://yandex.com/images-apphost/image-download"
SEARCH_URL = "https://yandex.com/images/search"
JSON_BLOCKS = '{"blocks":[{"block":"b-page_type_search-by-image__link"}]}'


class YandexProvider(Provider):
    id = "yandex"
    label = "Yandex Images"
    help = "Free, no key. Strong at finding re-uploads and mirrors."

    async def search(self, client: httpx.AsyncClient, image: bytes, meta: dict) -> ProviderOutput:
        problems: list[str] = []
        query_url = ""
        page_html = ""

        cbir_id = await self._upload_cbir(client, image)
        if cbir_id:
            query_url = f"{SEARCH_URL}?rpt=imageview&cbir_id={cbir_id}"
            page_html = await self._fetch(client, {"rpt": "imageview", "cbir_id": cbir_id})
            if not _find_sites(page_html):
                # the "sites" tab is more targeted; try it before giving up
                alt = await self._fetch(client, {"rpt": "imageview", "cbir_id": cbir_id,
                                                 "cbir_page": "sites"})
                if len(_find_sites(alt)) > 0:
                    page_html = alt
        else:
            problems.append("cbir upload failed")

        if not _find_sites(page_html):
            # path 2: the older JSON-block API returns the query string directly
            fallback_url = await self._upload_json_blocks(client, image)
            if fallback_url:
                alt = await self._fetch_url(client, fallback_url)
                if _find_sites(alt) or urls_in_html(alt):
                    page_html = alt
                    query_url = fallback_url
                else:
                    problems.append("json upload: empty results")
            else:
                problems.append("json upload failed")

        found = _find_sites(page_html)
        if not found:
            for url in urls_in_html(page_html):
                found.append({"url": url, "title": "", "thumb": ""})

        if not found:
            reason = "; ".join(problems) or "no matches on the results page"
            return ProviderOutput(error=reason)

        merged: dict[str, dict[str, Any]] = {}
        for site in found:
            url = clean_url(str(site.get("url") or ""))
            if not url.startswith("http") or not is_interesting(url):
                continue
            key = canonicalize(url)
            item = merged.setdefault(key, {"url": key, "title": "", "thumb": "", "mentions": 0})
            item["mentions"] += 1
            if not item["title"] and site.get("title"):
                item["title"] = str(site["title"]).strip()
            if not item["thumb"] and site.get("thumb"):
                item["thumb"] = str(site["thumb"]).strip()

        if not merged:
            return ProviderOutput(error="no usable links on the results page")

        ranked = sorted(merged.values(), key=lambda r: r["mentions"], reverse=True)[:60]
        results = [
            RawResult(provider=self.id, url=item["url"], title=item["title"],
                      thumb=item["thumb"], mentions=item["mentions"])
            for item in ranked
        ]
        note = f"{len(results)} match link{'s' if len(results) != 1 else ''}"
        if problems:
            note += f" · {', '.join(problems)}"
        return ProviderOutput(results=results, note=note, query_url=query_url,
                              raw_count=len(found), tags=_find_tags(page_html))

    # ------------------------------------------------------------------ #
    async def _upload_cbir(self, client: httpx.AsyncClient, image: bytes) -> str:
        try:
            resp = await client.post(
                UPLOAD_URL,
                params={
                    "cbir_id": "",
                    "images_avatars_size": "preview",
                    "images_avatars_namespace": "images-cbir",
                },
                files={"upfile": ("blob", image, "image/jpeg")},
                headers={"Referer": "https://yandex.com/images/"},
            )
            return str(resp.json().get("cbir_id") or "")
        except Exception:
            return ""

    async def _upload_json_blocks(self, client: httpx.AsyncClient, image: bytes) -> str:
        try:
            resp = await client.post(
                SEARCH_URL,
                params={"rpt": "imageview", "format": "json", "request": JSON_BLOCKS},
                files={"upfile": ("blob", image, "image/jpeg")},
                headers={"Referer": "https://yandex.com/images/"},
            )
            payload = resp.json()
            params = payload["blocks"][0]["params"]["url"]
            if isinstance(params, str) and params:
                return f"{SEARCH_URL}?{params.lstrip('?')}"
        except Exception:
            return ""
        return ""

    async def _fetch(self, client: httpx.AsyncClient, params: dict) -> str:
        try:
            resp = await client.get(SEARCH_URL, params=params,
                                    headers={"Referer": "https://yandex.com/images/"})
            return resp.text or ""
        except httpx.HTTPError:
            return ""

    async def _fetch_url(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            resp = await client.get(url, headers={"Referer": "https://yandex.com/images/"})
            return resp.text or ""
        except httpx.HTTPError:
            return ""


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _data_states(page_html: str) -> Iterable[Any]:
    """Yield parsed JSON from every data-state/data-bem attribute.

    Attribute values are HTML-escaped in the source, so the raw text is
    matched first and unescaped afterwards — unescaping first would truncate
    the value at its first inner quote.
    """
    for match in re.finditer(r"""data-(?:state|bem)=(?:"([^"]*)"|'([^']*)')""", page_html):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if not raw or len(raw) < 60:
            continue
        try:
            yield json.loads(html_mod.unescape(raw))
        except Exception:
            continue


def _find_sites(page_html: str) -> list[dict]:
    if not page_html:
        return []
    out: list[dict] = []
    seen: set[str] = set()

    for state in _data_states(page_html):
        # documented modern shape first; the generic walker is only a fallback
        init = state.get("initialState") if isinstance(state, dict) else None
        candidates: list = []
        if isinstance(init, dict):
            cbir_sites = init.get("cbirSites") or {}
            if isinstance(cbir_sites, dict):
                candidates.extend(cbir_sites.get("sites") or [])
            candidates.extend(init.get("sites") or [])
        if not candidates:
            candidates = _walk_sites(state)
        for site in candidates:
            if not isinstance(site, dict):
                continue
            url = site.get("url") or site.get("origUrl") or ""
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            if url in seen:
                continue
            seen.add(url)
            thumb = site.get("thumb") or site.get("thumbnail") or ""
            if isinstance(thumb, dict):
                thumb = thumb.get("url", "")
            out.append({"url": url, "title": str(site.get("title") or ""), "thumb": str(thumb or "")})
    return out


def _find_tags(page_html: str) -> list[str]:
    """Yandex's own guess at *what* is in the frame ("The Dark Knight", "cat"…).

    Handy when you don't even know which film/show a clip is from.
    """
    tags: list[str] = []
    for state in _data_states(page_html):
        init = state.get("initialState") if isinstance(state, dict) else None
        if not isinstance(init, dict):
            continue
        for key in ("cbirTags", "tags"):
            block = init.get(key)
            if isinstance(block, dict):
                items = block.get("tags") or block.get("items") or []
            elif isinstance(block, list):
                items = block
            else:
                items = []
            for item in items:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("name") or ""
                    if text and str(text) not in tags:
                        tags.append(str(text).strip())
                elif isinstance(item, str) and item not in tags:
                    tags.append(item.strip())
        # entity links such as "movie: The Dark Knight"
        for entity in _walk_sites(state):
            name = entity.get("name") or entity.get("text")
            kind = entity.get("entityType") or ""
            if isinstance(name, str) and isinstance(kind, str) and kind and name:
                label = f"{kind}: {name}"
                if label not in tags:
                    tags.append(label)
    return [t for t in tags if 0 < len(t) <= 60][:8]


def _walk_sites(node: Any, depth: int = 0) -> list[dict]:
    """Find site-like dicts anywhere in a blob (schema drift insurance)."""
    found: list[dict] = []
    if depth > 8:
        return found
    if isinstance(node, dict):
        if ("url" in node or "origUrl" in node) and ("title" in node or "domain" in node):
            if isinstance(node.get("url") or node.get("origUrl"), str):
                found.append(node)
        for value in node.values():
            found.extend(_walk_sites(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_sites(item, depth + 1))
    return found
