"""Search orchestration: fan out to every provider, merge, enrich, rank, save."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable

from .. import db
from .base import Provider, ProviderOutput, RawResult, canonicalize, classify, host_of, make_client
from .bing import BingProvider
from .demo import DemoProvider
from .enrich import enrich_many
from .lens import LensProvider
from .rank import rank
from .yandex import YandexProvider

PROVIDERS: dict[str, Provider] = {
    p.id: p for p in (LensProvider(), YandexProvider(), BingProvider(), DemoProvider())
}

FREE_PROVIDERS = ["lens", "yandex"]
MAX_ENRICH = 14


def provider_status(settings: dict) -> list[dict]:
    out = []
    for provider in PROVIDERS.values():
        available = provider.available(settings)
        out.append({
            "id": provider.id,
            "label": provider.label,
            "help": provider.help,
            "available": available,
            "reason": provider.unavailable_reason(settings),
            "key_setting": provider.key_setting,
            "free": provider.key_setting is None and provider.id != "demo",
        })
    return out


def _resolve_providers(settings: dict, requested: Iterable[str] | None) -> list[str]:
    ids = list(requested) if requested else list(settings.get("providers") or FREE_PROVIDERS)
    if settings.get("demo_mode"):
        ids.append("demo")
    chosen = []
    for pid in ids:
        provider = PROVIDERS.get(pid)
        if provider and provider.available(settings) and pid not in chosen:
            chosen.append(pid)
    return chosen


async def _run_one(client, provider: Provider, frames: list[bytes], meta: dict,
                   timeout: float) -> tuple[ProviderOutput, int]:
    started = time.perf_counter()
    try:
        output = await asyncio.wait_for(provider.search(client, frames[0], meta), timeout=timeout)
    except asyncio.TimeoutError:
        output = ProviderOutput(error=f"timed out after {int(timeout)}s")
    except Exception as exc:  # noqa: BLE001
        output = ProviderOutput(error=f"{type(exc).__name__}: {exc}"[:300])
    return output, int((time.perf_counter() - started) * 1000)


async def run_search(
    clip_id: int,
    frames: list[bytes],
    meta: dict,
    settings: dict,
    provider_ids: Iterable[str] | None = None,
    depth: int = 1,
) -> dict:
    """Run the reverse search for one clip and persist whatever comes back."""
    chosen = _resolve_providers(settings, provider_ids)
    timeout = float(settings.get("search_timeout") or 30)
    depth = max(1, min(int(depth or 1), 3))
    # first frame always, extra frames only when the user asked for a deep search
    search_frames = frames[:depth] if frames else []
    if not search_frames:
        return {"ok": False, "error": "no frames to search", "providers": [], "candidates": []}

    runs: list[dict] = []
    query_urls: dict[str, str] = {}
    recognition: list[str] = []
    collected: list[RawResult] = []

    async with make_client(timeout=timeout) as client:
        for frame_idx in range(len(search_frames)):
            image = search_frames[frame_idx]
            tasks = []
            for pid in chosen:
                provider = PROVIDERS[pid]
                run_id = db.start_run(clip_id, pid if len(search_frames) == 1 else f"{pid}#f{frame_idx}")
                tasks.append((pid, run_id, asyncio.create_task(
                    _run_one(client, provider, [image], {**meta, "frame": frame_idx}, timeout))))
            for pid, run_id, task in tasks:
                output, elapsed = await task
                db.finish_run(run_id, "ok" if output.ok else "error", len(output.results),
                              elapsed, output.error)
                runs.append({
                    "provider": PROVIDERS[pid].label,
                    "id": pid,
                    "status": "ok" if output.ok else "error",
                    "found": len(output.results),
                    "ms": elapsed,
                    "note": output.note,
                    "error": output.error,
                    "frame": frame_idx,
                })
                if output.query_url:
                    query_urls.setdefault(pid, output.query_url)
                for tag in output.tags or []:
                    if tag not in recognition:
                        recognition.append(tag)
                collected.extend(output.results)

    merged = _merge(collected)

    # order by raw evidence first, then fetch real titles for the top slice
    merged.sort(key=lambda c: (len(c["providers"]), c["mentions"]), reverse=True)
    enrich_targets = [c["url"] for c in merged[:MAX_ENRICH] if not c["title"] and c["url"].startswith("http")]
    if enrich_targets and settings.get("enrich_results", True):
        async with make_client(timeout=8) as client:
            enriched = await enrich_many(client, enrich_targets, limit=MAX_ENRICH)
        for cand in merged:
            info = enriched.get(cand["url"])
            if not info:
                continue
            if info.get("title"):
                cand["title"] = info["title"]
            if info.get("author"):
                cand["author"] = info["author"]
            if info.get("thumb"):
                cand["thumb"] = info["thumb"]
            if info.get("published"):
                cand["published"] = info["published"]
            if info.get("note"):
                cand["enrich_note"] = info["note"]

    ranked = rank(merged, bool(settings.get("prefer_full_video", True)))
    saved = _persist(clip_id, ranked)

    best = next((c for c in saved if c["score"] >= 40), None)
    db.update_clip(clip_id, {
        "status": "sourced" if saved else "no_results",
        "last_searched": db.now(),
        "best_source": best["url"] if best else "",
    })

    return {
        "ok": True,
        "clip_id": clip_id,
        "providers": runs,
        "query_urls": query_urls,
        "recognition": recognition[:8],
        "candidates": saved,
        "clip": db.get_clip(clip_id),
    }


def _merge(results: Iterable[RawResult]) -> list[dict]:
    merged: dict[str, dict[str, Any]] = {}
    for res in results:
        url = canonicalize(res.url)
        if not url.startswith("http"):
            continue
        entry = merged.get(url)
        if entry is None:
            kind, label = classify(url)
            entry = merged[url] = {
                "url": url,
                "host": host_of(url),
                "host_label": label,
                "kind": kind,
                "providers": [],
                "mentions": 0,
                "title": "",
                "author": res.extra.get("author", "") if res.extra else "",
                "thumb": "",
                "published": "",
                "enrich_note": "",
            }
        if res.provider not in entry["providers"]:
            entry["providers"].append(res.provider)
        entry["mentions"] += int(res.mentions or 1)
        if res.title and not entry["title"]:
            entry["title"] = res.title
        if res.thumb and not entry["thumb"]:
            entry["thumb"] = res.thumb
        if res.published and not entry["published"]:
            entry["published"] = res.published
        hint = (res.extra or {}).get("kind_hint")
        if hint:
            entry["kind"] = hint
    return list(merged.values())


def _persist(clip_id: int, cands: list[dict]) -> list[dict]:
    for cand in cands:
        provider = ",".join(cand["providers"])
        existing = db.candidate_exists(clip_id, cand["url"])
        if existing:
            db.update_candidate(_candidate_id(clip_id, cand["url"]), {
                "score": cand["score"],
                "reasons": cand["reasons"],
                "mentions": cand["mentions"],
                "title": cand["title"],
                "author": cand["author"],
                "thumb": cand["thumb"],
                "published": cand["published"],
            })
            continue
        db.add_candidate(
            clip_id,
            provider=provider,
            url=cand["url"],
            canonical=cand["url"],
            host=cand["host"],
            title=cand["title"],
            author=cand["author"],
            published=cand["published"],
            kind=cand["kind"],
            thumb=cand["thumb"],
            score=cand["score"],
            reasons=cand["reasons"],
            mentions=cand["mentions"],
        )
    clip = db.get_clip(clip_id)
    return [c for c in (clip or {}).get("candidates", [])]


def _candidate_id(clip_id: int, canonical: str) -> int:
    with db.tx() as conn:
        row = conn.execute(
            "SELECT id FROM candidates WHERE clip_id=? AND canonical=?", (clip_id, canonical)
        ).fetchone()
        return int(row["id"]) if row else 0
