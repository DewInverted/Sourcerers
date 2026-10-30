"""Sample results so the UI can be previewed with no network access.

Everything produced here is clearly tagged ``demo`` and the interface shows a
warning banner; nothing pretends to be a real match.
"""

from __future__ import annotations

import httpx

from .base import Provider, ProviderOutput, RawResult

SAMPLES = [
    ("https://example.com/demo/original-full-video", "Example Channel — full original upload", "video", 9),
    ("https://example.com/demo/mirror-upload", "Example mirror re-upload", "video", 4),
    ("https://example.com/demo/stock-library", "Example Stock Library — source footage", "stock", 2),
    ("https://example.com/demo/blog-post", "Example blog post featuring the clip", "page", 1),
]


class DemoProvider(Provider):
    id = "demo"
    label = "Demo sample data"
    help = "Preview only — produces placeholder rows so you can see the layout."
    requires_network = False

    async def search(self, client: httpx.AsyncClient, image: bytes, meta: dict) -> ProviderOutput:
        results = [
            RawResult(provider=self.id, url=url, title=title, mentions=mentions,
                      extra={"kind_hint": kind, "demo": True})
            for url, title, kind, mentions in SAMPLES
        ]
        return ProviderOutput(results=results, note="4 sample rows (no real search performed)")
