from __future__ import annotations

from app.search.base import (
    canonicalize,
    classify,
    host_of,
    is_interesting,
    unwrap_redirect,
    urls_in_html,
)
from app.search.rank import rank, score_candidate
from app.search.yandex import _find_sites


def test_youtube_variants_collapse():
    a = canonicalize("https://youtu.be/dQw4w9WgXcQ?si=tracking")
    b = canonicalize("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&feature=share")
    c = canonicalize("https://m.youtube.com/shorts/dQw4w9WgXcQ")
    assert a == b == c == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_social_canonicalisation():
    assert canonicalize("https://www.tiktok.com/@user/video/7300000000000000000?is_from_webapp=1") \
        == "https://www.tiktok.com/@user/video/7300000000000000000"
    assert canonicalize("https://www.instagram.com/reel/ABCdef123/?igsh=zzz") \
        == "https://www.instagram.com/reel/ABCdef123/"
    assert canonicalize("https://x.com/someone/status/123456789") == "https://x.com/i/status/123456789"


def test_tracking_params_stripped():
    url = canonicalize("https://example.com/watch?utm_source=google&id=7")
    assert "utm_source" not in url and "id=7" in url


def test_redirect_unwrapping():
    assert unwrap_redirect("https://www.google.com/url?q=https://vimeo.com/123&sa=U") == "https://vimeo.com/123"
    assert unwrap_redirect("https://yandex.com/images/search?url=https%3A//tiktok.com/x") == "https://tiktok.com/x"
    # a plain google link stays put
    assert unwrap_redirect("https://www.google.com/search?q=cats") == "https://www.google.com/search?q=cats"


def test_host_and_classification():
    assert host_of("https://www.YouTube.com/watch?v=x") == "youtube.com"
    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ")[0] == "video"
    assert classify("https://www.gettyimages.com/detail/123")[0] == "stock"
    assert classify("https://example.com/blog")[0] == "page"


def test_noise_is_filtered():
    assert not is_interesting("https://www.google.com/search?q=x")
    assert not is_interesting("https://ssl.gstatic.com/ui/logo.png")
    assert not is_interesting("https://example.com/app.js")
    assert is_interesting("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def test_escaped_html_extraction():
    html = (
        '<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">x</a>'
        ' https:\\/\\/www.tiktok.com\\/@user\\/video\\/7300000000000000000'
        ' "https://www.google.com/url?q=https://vimeo.com/999"'
        ' https://ssl.gstatic.com/logo.png'
    )
    urls = urls_in_html(html)
    assert any("youtube.com/watch?v=dQw4w9WgXcQ" in u for u in urls)
    assert any("tiktok.com/@user/video/7300000000000000000" in u for u in urls)
    assert "https://vimeo.com/999" in urls
    assert not any("gstatic" in u for u in urls)


def test_yandex_sites_extraction():
    payload = (
        '{"initialState":{"cbirSites":{"sites":['
        '{"title":"Original upload","url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","domain":"youtube.com"},'
        '{"title":"Mirror","url":"https://www.tiktok.com/@x/video/7300000000000000000","domain":"tiktok.com"}'
        ']}}}'
    ).replace('"', "&quot;")
    html = f'<div class="Root" data-state="{payload}"></div>'
    sites = _find_sites(html)
    assert len(sites) == 2
    assert sites[0]["title"] == "Original upload"


def test_ranking_prefers_full_videos():
    full = {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "kind": "video",
            "host_label": "youtube", "providers": ["lens", "yandex"], "mentions": 4,
            "title": "The original video", "author": "Channel"}
    short = {"url": "https://www.youtube.com/shorts/dQw4w9WgXcQ", "kind": "video",
             "host_label": "youtube", "providers": ["lens"], "mentions": 1,
             "title": "Ranking compilation #shorts", "author": ""}
    ranked = rank([short, full])
    assert ranked[0]["url"] == full["url"]
    assert ranked[0]["score"] > ranked[1]["score"]
    assert any("YouTube" in r for r in ranked[0]["reasons"])


def test_confirmed_source_wins():
    score, reasons = score_candidate({"url": "https://x.com/i/status/1", "confirmed": True})
    assert score >= 95
    assert "you marked" in reasons[0]


def test_demo_results_are_ranked_not_crash():
    cands = rank([{"url": "https://example.com/demo/stock-library", "kind": "stock",
                   "host_label": "stock", "providers": ["demo"], "mentions": 1, "title": ""}])
    assert 0 < cands[0]["score"] <= 100


def test_yandex_tag_extraction():
    payload = (
        '{"initialState":{"cbirTags":{"tags":[{"text":"The Dark Knight","url":"/x"},{"text":"movie"}]},'
        '"cbirSites":{"sites":[]}}}'
    ).replace('"', "&quot;")
    from app.search.yandex import _find_tags

    tags = _find_tags(f'<div data-state="{payload}"></div>')
    assert "The Dark Knight" in tags
    assert "movie" in tags


def test_broken_data_state_is_ignored():
    from app.search.yandex import _find_sites

    assert _find_sites('<div data-state="{not json}"></div>') == []
    assert _find_sites("") == []
