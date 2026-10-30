"""How likely is this link the original?

Nobody can prove provenance from a reverse image search, so instead of a
single opaque number we blend the evidence we actually have and show the
reasons next to each card.  The user stays in charge.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .base import host_of, youtube_id

REPOST_WORDS = re.compile(
    r"\b(compilation|compilations|reupload|re-upload|repost|re-edit|best of|"
    r"ranking|ranked|top \d+|tiktok|shorts|reaction|edit|edits|clips?|"
    r"part \d+|#shorts|highlights?)\b",
    re.I,
)
STOCK_WORDS = re.compile(r"\b(stock footage|royalty[- ]free|4k footage|clip library)\b", re.I)


def _is_short(url: str) -> bool:
    return "/shorts/" in url or "/reel/" in url or host_of(url).endswith("tiktok.com")


def _age_bonus(published: str) -> tuple[float, str]:
    if not published:
        return 0.0, ""
    raw = published.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        try:
            dt = datetime.strptime(published[:10], "%Y-%m-%d")
        except Exception:
            return 0.0, ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    years = (datetime.now(timezone.utc) - dt).days / 365.25
    if years >= 3:
        return 8.0, f"uploaded {int(years)} years ago — hard to be a repost of the clip"
    if years >= 1:
        return 5.0, "uploaded over a year ago"
    if years < 0.25:
        return -4.0, "very recent upload — may be a repost"
    return 0.0, ""


def score_candidate(cand: dict, prefer_full_video: bool = True) -> tuple[float, list[str]]:
    if cand.get("confirmed"):
        return 97.0, ["you marked this as the original"]

    providers = cand.get("providers") or []
    kind = cand.get("kind") or "page"
    label = cand.get("host_label") or ""
    title = cand.get("title") or ""
    url = cand.get("url") or ""
    mentions = int(cand.get("mentions") or 1)

    score = 26.0
    reasons: list[str] = []

    names = {"lens": "Google Lens", "yandex": "Yandex", "bing": "Bing", "demo": "Demo"}
    if len(providers) >= 2:
        score += 16
        reasons.append("seen by " + " + ".join(names.get(p, p) for p in providers))
    elif providers:
        score += 7
        reasons.append("found by " + names.get(providers[0], providers[0]))

    score += min(8.0, 2.2 * math.log2(mentions + 1))
    if mentions >= 3:
        reasons.append(f"appears {mentions}× on the results page")

    # what kind of page is it?
    if label == "youtube" and youtube_id(url):
        if "/shorts/" in url:
            score += 5
            reasons.append("YouTube Short — often a repost, not the source")
        else:
            score += 16 if prefer_full_video else 10
            reasons.append("full-length YouTube upload")
    elif kind == "stock":
        score += 15
        reasons.append("stock-footage library (a licensing-clean source)")
    elif label in {"archive"}:
        score += 11
        reasons.append("archive.org — original file often downloadable")
    elif kind == "video":
        score += 8
        reasons.append(f"video page on {label or host_of(url)}")
    elif kind == "image":
        score += 1
        reasons.append("image / gallery page, not a video")
    else:
        score += 3

    if STOCK_WORDS.search(title):
        score += 3
    if _is_short(url):
        score -= 2
    if REPOST_WORDS.search(title):
        score -= 6
        reasons.append("title reads like an edit/compilation (could be a repost)")

    if cand.get("author"):
        score += 3
    age_score, age_reason = _age_bonus(cand.get("published") or "")
    if age_score:
        score += age_score
        reasons.append(age_reason)

    if not cand.get("title"):
        reasons.append("no title available — open it to check")

    return max(1.0, min(96.0, score)), reasons


def rank(cands: list[dict], prefer_full_video: bool = True) -> list[dict]:
    for cand in cands:
        score, reasons = score_candidate(cand, prefer_full_video)
        cand["score"] = round(score, 1)
        cand["reasons"] = reasons
    return sorted(cands, key=lambda c: (c.get("confirmed", False), c["score"]), reverse=True)
