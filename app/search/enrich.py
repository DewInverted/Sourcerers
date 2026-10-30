"""Turn bare links into something you can judge at a glance.

A raw URL like ``youtube.com/watch?v=abc`` tells you nothing.  oEmbed (no key,
official) gives the real title + channel for YouTube and friends; for other
sites we read og:title.  Everything here is best-effort and time-boxed.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re

import httpx

from ..config import ENRICH_TIMEOUT
from .base import canonicalize, host_of, youtube_id

OEMBED_ENDPOINTS = {
    "youtube.com": "https://www.youtube.com/oembed",
    "vimeo.com": "https://vimeo.com/api/oembed.json",
    "soundcloud.com": "https://soundcloud.com/oembed",
    "dailymotion.com": "https://www.dailymotion.com/services/oembed",
}
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_RE = re.compile(r'<meta[^>]+(?:property|name)=["\']og:([a-z:_]+)["\'][^>]*content=["\']([^"\']+)', re.I)
OG_RE_REV = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:([a-z:_]+)', re.I)


def _clean_text(value: str) -> str:
    value = html_mod.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:300]


async def enrich_one(client: httpx.AsyncClient, url: str) -> dict:
    """Return {title, author, thumb, published, note} for a candidate URL."""
    host = host_of(url)
    out: dict = {"title": "", "author": "", "thumb": "", "published": "", "note": ""}

    if host.endswith("youtube.com") or host == "youtu.be":
        vid = youtube_id(url)
        if vid:
            try:
                resp = await client.get(
                    "https://www.youtube.com/oembed",
                    params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
                    timeout=ENRICH_TIMEOUT,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    out["title"] = _clean_text(data.get("title", ""))
                    out["author"] = _clean_text(data.get("author_name", ""))
                    out["thumb"] = data.get("thumbnail_url", "")
                    out["note"] = "youtube"
                    return out
                out["note"] = f"YouTube returned {resp.status_code}"
            except Exception:
                out["note"] = "YouTube metadata unavailable"
            return out

    for suffix, endpoint in OEMBED_ENDPOINTS.items():
        if host.endswith(suffix):
            try:
                resp = await client.get(endpoint, params={"url": url, "format": "json"},
                                        timeout=ENRICH_TIMEOUT)
                if resp.status_code == 200:
                    data = resp.json()
                    out["title"] = _clean_text(data.get("title", ""))
                    out["author"] = _clean_text(data.get("author_name", ""))
                    out["thumb"] = data.get("thumbnail_url", "")
                    return out
            except Exception:
                pass
            break

    # generic: fetch a little of the page and read its metadata
    try:
        resp = await client.get(
            url,
            timeout=ENRICH_TIMEOUT,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        body = resp.text[:300_000] if resp.status_code < 400 else ""
    except Exception:
        return out
    if not body:
        return out

    meta: dict[str, str] = {}
    for prop, content in OG_RE.findall(body):
        meta.setdefault(prop.lower(), _clean_text(content))
    for content, prop in OG_RE_REV.findall(body):
        meta.setdefault(prop.lower(), _clean_text(content))

    out["title"] = meta.get("title") or _clean_text((TITLE_RE.search(body).group(1) if TITLE_RE.search(body) else ""))
    out["author"] = meta.get("site_name", "")
    out["thumb"] = meta.get("image", "")
    out["published"] = meta.get("article:published_time", "") or meta.get("video:release_date", "")
    return out


async def enrich_many(client: httpx.AsyncClient, urls: list[str], limit: int = 12,
                      concurrency: int = 5) -> dict[str, dict]:
    targets = urls[:limit]
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    async def worker(url: str) -> None:
        async with sem:
            try:
                results[canonicalize(url)] = await enrich_one(client, url)
            except Exception:
                results[canonicalize(url)] = {}

    await asyncio.gather(*(worker(u) for u in targets))
    return results
