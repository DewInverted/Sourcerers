"""Shared plumbing for reverse-image-search providers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx

from ..config import USER_AGENT

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "igshid", "igsh", "si", "feature", "ref", "ref_src",
    "ref_url", "share_id", "is_from_webapp", "sender_device", "web_id",
    "pp", "app", "lang", "hl", "_r", "_t", "s", "t",
}

# hosts that are never an "original source"
BLOCKED_HOSTS = (
    "google.com", "googleusercontent.com", "gstatic.com", "googleapis.com",
    "googleadservices.com", "doubleclick.net", "googlesyndication.com",
    "google.co", "schema.org", "w3.org", "yandex.net", "yandex.com",
    "yandex.ru", "yastatic.net", "bing.com", "microsoft.com", "msn.com",
    "cloudflare.com", "amazonaws.com", "akamaihd.net", "jsdelivr.net",
    "fontawesome.com", "gravatar.com", "wp.com", "wordpress.org",
)

VIDEO_HOSTS = {
    "youtube.com": "youtube", "youtu.be": "youtube", "vimeo.com": "vimeo",
    "dailymotion.com": "dailymotion", "twitch.tv": "twitch", "kick.com": "kick",
    "rumble.com": "rumble", "odysee.com": "odysee", "bilibili.com": "bilibili",
    "streamable.com": "streamable", "bitchute.com": "bitchute",
    "facebook.com": "facebook", "fb.watch": "facebook", "tiktok.com": "tiktok",
    "instagram.com": "instagram", "twitter.com": "twitter", "x.com": "twitter",
    "snapchat.com": "snapchat", "threads.net": "threads", "reddit.com": "reddit",
    "redd.it": "reddit", "vk.com": "vk", "ok.ru": "ok.ru", "nicovideo.jp": "niconico",
    "bbc.co.uk": "page", "archive.org": "archive",
}

STOCK_HOSTS = {
    "gettyimages.com", "shutterstock.com", "istockphoto.com", "alamy.com",
    "adobe.com", "dreamstime.com", "123rf.com", "depositphotos.com",
    "pond5.com", "envato.com", "storyblocks.com", "videvo.net", "pexels.com",
    "pixabay.com", "unsplash.com", "vecteezy.com", "freepik.com", "stock.adobe.com",
}

IMAGE_HOSTS = {
    "pinterest.com", "pinimg.com", "imgur.com", "giphy.com", "tenor.com",
    "wikimedia.org", "wikipedia.org", "flickr.com", "deviantart.com",
    "artstation.com", "behance.net", "tumblr.com", "9gag.com", "ifunny.co",
}


def clean_url(url: str) -> str:
    url = url.strip().strip('"\'.,);]')
    url = url.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
    if url.startswith("//"):
        url = "https:" + url
    return url


def host_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def base_domain(host: str) -> str:
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # handle co.uk style suffixes
    if parts[-2] in {"co", "com", "org", "net", "gov", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def classify(url: str) -> tuple[str, str]:
    """Return (kind, label) where kind is video|stock|image|page."""
    host = host_of(url)
    domain = base_domain(host)
    if domain in STOCK_HOSTS or host.endswith("stock.adobe.com"):
        return "stock", "stock"
    if domain == "youtube.com" or host == "youtu.be":
        return "video", "youtube"
    if domain in VIDEO_HOSTS:
        return "video", VIDEO_HOSTS[domain]
    if domain in IMAGE_HOSTS:
        return "image", "image"
    return "page", "page"


def youtube_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = host_of(url)
    if host == "youtu.be":
        vid = parsed.path.strip("/").split("/")[0]
        return vid if re.fullmatch(r"[\w-]{11}", vid or "") else None
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            vid = parse_qs(parsed.query).get("v", [""])[0]
            return vid if re.fullmatch(r"[\w-]{11}", vid or "") else None
        m = re.match(r"^/(shorts|embed|live|v)/([\w-]{11})", parsed.path)
        if m:
            return m.group(2)
    return None


def canonicalize(url: str) -> str:
    """Stable key per result so the same video from 3 engines is one card."""
    url = clean_url(url)
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    host = host_of(url)
    vid = youtube_id(url)
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    if host.endswith("tiktok.com"):
        m = re.search(r"/video/(\d+)", parsed.path) or re.search(r"/photo/(\d+)", parsed.path)
        if m:
            return f"https://www.tiktok.com/@{parsed.path.split('/')[1].lstrip('@')}/video/{m.group(1)}"
    if host.endswith("instagram.com"):
        m = re.match(r"^/(reel|p|tv)/([\w-]+)", parsed.path)
        if m:
            return f"https://www.instagram.com/{m.group(1)}/{m.group(2)}/"
    if host.endswith("twitter.com") or host == "x.com":
        m = re.match(r"^/[^/]+/status/(\d+)", parsed.path)
        if m:
            return f"https://x.com/i/status/{m.group(1)}"
    if host.endswith("reddit.com"):
        m = re.match(r"^/r/[^/]+/comments/(\w+)", parsed.path)
        if m:
            return f"https://www.reddit.com/comments/{m.group(1)}"
    query = parse_qs(parsed.query, keep_blank_values=False)
    kept = {k: v for k, v in query.items() if k.lower() not in TRACKING_PARAMS}
    from urllib.parse import urlencode

    new_query = urlencode({k: v[0] for k, v in kept.items()})
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme or "https", host, path, "", new_query, ""))


REDIRECT_HOSTS = ("google.com", "google.co", "yandex.com", "yandex.ru", "bing.com")


def unwrap_redirect(url: str, depth: int = 0) -> str:
    """Turn ``google.com/url?q=<real>`` / yandex ``/images/search?url=<real>`` into <real>."""
    if depth > 2:
        return url
    host = host_of(url)
    if not any(host == h or host.endswith("." + h) for h in REDIRECT_HOSTS):
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    params = parse_qs(parsed.query)
    for key in ("q", "url", "imgurl", "u", "rpt_url"):
        values = params.get(key)
        if not values:
            continue
        target = values[0]
        if target.startswith(("http://", "https://")) and host_of(target) and not any(
            host_of(target) == h or host_of(target).endswith("." + h) for h in REDIRECT_HOSTS
        ):
            return unwrap_redirect(target, depth + 1)
    return url


def is_interesting(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    host = host_of(url)
    if not host or "." not in host:
        return False
    if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
        return False
    if re.search(r"\.(js|css|png|jpe?g|webp|svg|gif|ico|woff2?|ttf|mp4|m3u8|json|xml)(\?|$)",
                 urlparse(url).path, re.I):
        return False
    if "/url?q=" in url or "aclk?" in url or "/pagead/" in url:
        return False
    if len(url) > 900:
        return False
    return True


URL_RE = re.compile(r"https?://[^\s\"'<>\\\]\[{}|^`]+")


def urls_in_html(html: str) -> list[str]:
    """Pull candidate page URLs out of a search results page (escaped or not)."""
    text = (
        html.replace("\\u003d", "=")
        .replace("\\u0026", "&")
        .replace("\\u003f", "?")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )
    found = []
    for match in URL_RE.finditer(text):
        url = unwrap_redirect(clean_url(match.group(0)))
        if is_interesting(url):
            found.append(url)
    return found


# --------------------------------------------------------------------------- #
# provider contract
# --------------------------------------------------------------------------- #
@dataclass
class RawResult:
    provider: str
    url: str
    title: str = ""
    thumb: str = ""
    mentions: int = 1
    published: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderOutput:
    results: list[RawResult] = field(default_factory=list)
    note: str = ""
    query_url: str = ""
    raw_count: int = 0
    error: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


class Provider:
    id: str = "base"
    label: str = "Base"
    key_setting: str | None = None
    help: str = ""
    requires_network: bool = True

    def available(self, settings: dict) -> bool:
        if self.key_setting:
            return bool(str(settings.get(self.key_setting, "")).strip())
        return True

    def unavailable_reason(self, settings: dict) -> str:
        if self.key_setting and not str(settings.get(self.key_setting, "")).strip():
            return f"needs an API key ({self.key_setting})"
        return ""

    async def search(self, client: httpx.AsyncClient, image: bytes,
                     meta: dict) -> ProviderOutput:  # pragma: no cover - interface
        raise NotImplementedError


def make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
        timeout=httpx.Timeout(timeout, connect=10.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
    )
