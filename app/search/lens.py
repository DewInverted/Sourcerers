"""Google Lens reverse image search (no API key).

Google has no public Lens API, so we do what a browser does: upload the frame
to the Lens endpoint, follow the redirect to the results page, then pull the
page links out of the server-rendered payload.  Three endpoints are tried in
order because Google rotates these.  If all of them fail the UI still offers
the "open in browser" escape hatch.
"""

from __future__ import annotations

import re
import time
from collections import Counter

import httpx

from .base import Provider, ProviderOutput, RawResult, canonicalize, urls_in_html

BLOCK_MARKERS = (
    "unusual traffic", "captcha", "detected unusual", "before you continue",
    "consent.google.com", "enablejs", "our systems have detected",
)


class LensProvider(Provider):
    id = "lens"
    label = "Google Lens"
    help = "Free, no key. Uses the public Lens upload endpoint."

    ENDPOINTS = (
        ("https://lens.google.com/v3/upload", "v3"),
        ("https://lens.google.com/upload", "legacy"),
        ("https://www.google.com/searchbyimage/upload", "sbi"),
    )

    async def search(self, client: httpx.AsyncClient, image: bytes, meta: dict) -> ProviderOutput:
        problems: list[str] = []
        for endpoint, mode in self.ENDPOINTS:
            try:
                output = await self._attempt(client, endpoint, mode, image, meta)
            except httpx.TimeoutException:
                problems.append(f"{mode}: timed out")
                continue
            except httpx.HTTPError as exc:
                problems.append(f"{mode}: {type(exc).__name__}")
                continue
            except Exception as exc:  # noqa: BLE001 - scrapers fail in weird ways
                problems.append(f"{mode}: {exc}")
                continue
            if output.results:
                if problems:
                    output.note = f"{output.note} · fallback used ({', '.join(problems)})"
                return output
            if output.error:
                problems.append(f"{mode}: {output.error}")
        return ProviderOutput(error="; ".join(problems) or "Lens returned no usable page links")

    # ------------------------------------------------------------------ #
    async def _attempt(self, client: httpx.AsyncClient, endpoint: str, mode: str,
                       image: bytes, meta: dict) -> ProviderOutput:
        width = meta.get("width") or 0
        height = meta.get("height") or 0
        data = {"processed_image_dimensions": f"{width},{height}"} if mode == "v3" else {}
        if mode in ("legacy", "sbi"):
            data["image_content"] = ""
        params = {"hl": "en"}
        if mode == "v3":
            params["stcs"] = str(int(time.time() * 1000))
            params["ep"] = "gsbubb"

        files = {"encoded_image": ("frame.jpg", image, "image/jpeg")}
        headers = {
            "Referer": "https://lens.google.com/",
            "Origin": "https://lens.google.com",
        }
        resp = await client.post(endpoint, params=params, files=files, data=data, headers=headers)
        html = resp.text or ""
        query_url = str(resp.url)

        # Lens sometimes answers with a stub; the real payload is at the redirect target.
        if len(html) < 2000 or not any(marker in html for marker in ("AF_initDataCallback", "lens", "Visual matches")):
            try:
                follow = await client.get(query_url)
                if len(follow.text or "") > len(html):
                    html = follow.text
                    query_url = str(follow.url)
            except httpx.HTTPError:
                pass

        lowered = html.lower()
        if len(html) < 800 or any(marker in lowered for marker in BLOCK_MARKERS[:4]):
            if "captcha" in lowered or "unusual traffic" in lowered:
                return ProviderOutput(error="Google rate-limited this machine (captcha)")
            if resp.status_code >= 400:
                return ProviderOutput(error=f"HTTP {resp.status_code}")
            return ProviderOutput(error="empty results page")

        urls = urls_in_html(html)
        if not urls:
            return ProviderOutput(error="results page had no external links")

        counts = Counter(canonicalize(u) for u in urls)
        results = [
            RawResult(provider=self.id, url=canonical, mentions=count)
            for canonical, count in counts.most_common(60)
        ]
        note = f"{len(results)} visual match link{'s' if len(results) != 1 else ''}"
        return ProviderOutput(results=results, note=note, query_url=query_url, raw_count=len(urls))


def _extract_titles(html: str) -> list[str]:
    """Lens embeds page titles as short quoted strings next to matches."""
    titles = re.findall(r'\[null,null,"([^"\\]{8,120})"\]', html)
    return [t for t in titles if not t.startswith("http")]
