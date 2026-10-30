"""Bing Visual Search (optional, needs a free Azure key)."""

from __future__ import annotations

import httpx

from .base import Provider, ProviderOutput, RawResult, canonicalize, clean_url, is_interesting

ENDPOINT = "https://api.bing.microsoft.com/v7.0/images/visualsearch"
KNOWLEDGE = '{"invokedSkills":["SimilarImages"],"knowledgeRequest":{"invokedSkills":["SimilarImages"]}}'


class BingProvider(Provider):
    id = "bing"
    label = "Bing Visual Search"
    key_setting = "bing_key"
    help = "Optional. Free tier key from portal.azure.com → Bing Search → Visual Search."

    async def search(self, client: httpx.AsyncClient, image: bytes, meta: dict) -> ProviderOutput:
        key = str(meta.get("bing_key") or "").strip()
        if not key:
            return ProviderOutput(error="no Bing key configured")
        try:
            resp = await client.post(
                ENDPOINT,
                headers={"Ocp-Apim-Subscription-Key": key},
                files={
                    "image": ("frame.jpg", image, "image/jpeg"),
                    "knowledgeRequest": (None, KNOWLEDGE, "application/json"),
                },
            )
        except httpx.HTTPError as exc:
            return ProviderOutput(error=f"request failed ({type(exc).__name__})")

        if resp.status_code == 401:
            return ProviderOutput(error="Bing rejected the API key")
        if resp.status_code == 429:
            return ProviderOutput(error="Bing rate limit hit")
        if resp.status_code >= 400:
            return ProviderOutput(error=f"Bing HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            return ProviderOutput(error="Bing returned a non-JSON response")

        results: list[RawResult] = []
        seen: set[str] = set()
        for tag in payload.get("tags", []) or []:
            for action in tag.get("actions", []) or []:
                data = action.get("data") or {}
                for value in (data.get("value") or []) + (data.get("similarImages") or []):
                    url = clean_url(str(value.get("hostPageUrl") or value.get("contentUrl") or ""))
                    if not url or not is_interesting(url):
                        continue
                    canonical = canonicalize(url)
                    if canonical in seen:
                        continue
                    seen.add(canonical)
                    results.append(
                        RawResult(
                            provider=self.id,
                            url=canonical,
                            title=str(value.get("name") or ""),
                            thumb=str(value.get("thumbnailUrl") or ""),
                            published=str(value.get("datePublished") or ""),
                            extra={"author": value.get("creator", {}).get("name", "") if isinstance(
                                value.get("creator"), dict) else ""},
                        )
                    )
        if not results:
            return ProviderOutput(error="Bing found no matching pages")
        return ProviderOutput(results=results[:60], note=f"{len(results)} match links")
