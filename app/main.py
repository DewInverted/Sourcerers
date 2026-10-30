"""Sourcerers — local source-finder for short-form video editors."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, media
from .hashing import Fingerprint
from .search import PROVIDERS, provider_status, run_search
from .search.base import canonicalize, classify, host_of, is_interesting

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, docs_url="/api/docs",
              lifespan=lifespan)

app.mount("/clips", StaticFiles(directory=str(config.CLIPS_DIR)), name="clips")
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
JOBS: dict[str, dict[str, Any]] = {}
_job_lock = threading.Lock()


def new_job(kind: str, **extra: Any) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _job_lock:
        JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "running",
            "stage": "queued",
            "message": "starting…",
            "progress": 0,
            "result": None,
            "error": "",
            "created": time.time(),
            **extra,
        }
    _prune_jobs()
    return job_id


def job_update(job_id: str, **patch: Any) -> None:
    with _job_lock:
        job = JOBS.get(job_id)
        if job:
            job.update(patch)


def job_get(job_id: str) -> dict | None:
    with _job_lock:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _prune_jobs() -> None:
    cutoff = time.time() - 3600
    for key in [k for k, v in JOBS.items() if v["created"] < cutoff]:
        JOBS.pop(key, None)


def spawn(coro) -> None:
    asyncio.create_task(coro)


# --------------------------------------------------------------------------- #
# startup
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((config.STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/state")
async def state() -> dict:
    settings = db.get_settings()
    return {
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "settings": settings,
        "providers": provider_status(settings),
        "capabilities": {
            "ffmpeg": bool(config.FFMPEG),
            "ytdlp": bool(config.YTDLP),
            "data_dir": str(config.DATA_DIR),
        },
        "stats": db.stats(),
    }


class SettingsPatch(BaseModel):
    frames: int | None = None
    dup_mean_distance: float | None = None
    dup_coverage: float | None = None
    providers: list[str] | None = None
    bing_key: str | None = None
    search_timeout: float | None = None
    enrich_results: bool | None = None
    prefer_full_video: bool | None = None
    demo_mode: bool | None = None


@app.post("/api/settings")
async def save_settings(patch: SettingsPatch) -> dict:
    values = {k: v for k, v in patch.model_dump().items() if v is not None}
    if "frames" in values:
        values["frames"] = max(1, min(int(values["frames"]), config.MAX_FRAME_COUNT))
    if "dup_mean_distance" in values:
        values["dup_mean_distance"] = max(0.05, min(float(values["dup_mean_distance"]), 0.5))
    if "dup_coverage" in values:
        values["dup_coverage"] = max(0.1, min(float(values["dup_coverage"]), 1.0))
    settings = db.set_settings(values)
    return {"ok": True, "settings": settings, "providers": provider_status(settings)}


# --------------------------------------------------------------------------- #
# analyze (upload / url) -> job
# --------------------------------------------------------------------------- #
MEDIA_SUFFIXES = {s.lower() for s in (media.VIDEO_EXT | media.IMAGE_EXT)}


def _pretty_title(label: str) -> str:
    """Turn a filename or URL into something readable in the library."""
    stem = label
    suffix = Path(stem).suffix.lower()
    if suffix in MEDIA_SUFFIXES:
        stem = stem[: -len(suffix)]
    if stem.startswith("http"):
        from urllib.parse import urlparse

        parsed = urlparse(label if "://" in label else "https://" + label)
        vid = parsed.query.split("v=")[-1].split("&")[0] if "v=" in parsed.query else ""
        stem = f"{parsed.netloc.replace('www.', '')} clip {vid}".strip()
    stem = re.sub(r"\s+", " ", stem.replace("_", " ")).strip(" -")
    return (stem or "clip")[:90]


def _safe_name(name: str) -> str:
    name = Path(name or "clip").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]
    return name or "clip"


async def _analyze_job(job_id: str, tmp_path: Path | None, url: str, want_frames: int,
                       title: str, start: float) -> None:
    settings = db.get_settings()
    frames_wanted = want_frames or int(settings.get("frames") or config.DEFAULT_FRAME_COUNT)
    label = title or (tmp_path.name if tmp_path else url)
    pretty = label
    work: Path | None = tmp_path
    try:
        if url:
            job_update(job_id, stage="download", message="downloading clip with yt-dlp…", progress=8)
            if not config.YTDLP:
                raise media.MediaError(
                    "yt-dlp is not installed. Run 'pip install yt-dlp', or download the clip "
                    "and drag the file in instead."
                )
            dest = config.TMP_DIR / f"{uuid.uuid4().hex}.%(ext)s"
            work = await asyncio.to_thread(media.download_video, url, dest)
            label = url[:160]
            pretty = url

        if work is None:
            raise media.MediaError("no file or link provided")

        job_update(job_id, stage="frames", message="sampling frames…", progress=24)
        extracted = await asyncio.to_thread(media.extract_file, work, frames_wanted)

        job_update(job_id, stage="fingerprint", message="building visual fingerprint…", progress=46)
        fingerprint = await asyncio.to_thread(extracted.fingerprint)

        job_update(job_id, stage="dedup", message="checking your library for repeats…", progress=60)
        matches = await asyncio.to_thread(db.similar_clips, fingerprint, None, settings)
        top = matches[0] if matches else None
        verdict = top["comparison"]["verdict"] if top else "distinct"
        cluster_id = None
        if top and verdict == "duplicate":
            cluster_id = top.get("cluster_id") or top["id"]

        clip = db.create_clip(
            title="",
            source_label=label,
            source_kind=extracted.kind,
            fingerprint=fingerprint,
            thumb="",
            duration=extracted.duration,
            width=extracted.width,
            height=extracted.height,
            cluster_id=cluster_id,
        )
        clip_id = clip["id"]
        clip_dir = config.CLIPS_DIR / str(clip_id)
        names = await asyncio.to_thread(media.save_frames, extracted.frames, clip_dir / "frames")

        # thumbnail = middle frame, downscaled
        from PIL import Image

        mid = extracted.frames[len(extracted.frames) // 2]
        with Image.open(io.BytesIO(mid.data)) as im:
            im = im.convert("RGB")
            w = min(480, im.width)
            thumb = im.resize((w, max(2, int(im.height * w / im.width))))
            clip_dir.mkdir(parents=True, exist_ok=True)
            thumb.save(clip_dir / "thumb.jpg", "JPEG", quality=86)

        default_title = _pretty_title(pretty or "clip")
        db.update_clip(clip_id, {
            "thumb": f"{clip_id}/thumb.jpg",
            "title": default_title,
            "verdict": verdict if top else "",
            "match_score": top["comparison"]["score"] if top else 0,
            "status": "duplicate" if verdict == "duplicate" else "analyzed",
        })

        result = {
            "clip": db.get_clip(clip_id),
            "dedup": {"verdict": verdict, "match": top, "others": matches[1:], "cluster_id": cluster_id},
            "frames": [f"{clip_id}/frames/{n}" for n in names],
            "compare_url": None,
        }
        job_update(job_id, status="done", stage="done", progress=100,
                   message="ready", result=result)
    except media.MediaError as exc:
        job_update(job_id, status="error", error=str(exc), message=str(exc), stage="error")
    except Exception as exc:  # noqa: BLE001
        job_update(job_id, status="error", error=f"{type(exc).__name__}: {exc}",
                   message="something went wrong", stage="error")
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        if url and work and work.exists():
            try:
                work.unlink()
            except OSError:
                pass


@app.post("/api/analyze")
async def analyze(
    file: UploadFile | None = File(None),
    url: str = Form(""),
    frames: int = Form(0),
    title: str = Form(""),
) -> dict:
    url = (url or "").strip()
    if not file and not url:
        raise HTTPException(400, "drop a file or paste a video link")

    tmp_path: Path | None = None
    if file is not None:
        suffix = Path(file.filename or "clip.mp4").suffix or ".mp4"
        tmp_path = config.TMP_DIR / f"{uuid.uuid4().hex}{suffix}"
        size = 0
        with open(tmp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 2 * 1024 * 1024 * 1024:
                    out.close()
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(413, "file is larger than 2 GB")
                out.write(chunk)
        if size == 0:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(400, "that file was empty")
    # keep the caller's filename as the human label, not our temp uuid
    label = title or (file.filename if file is not None else url)
    job_id = new_job("analyze")
    spawn(_analyze_job(job_id, tmp_path, url, frames, label, 0.0))
    return {"job_id": job_id}


@app.post("/api/compare")
async def compare_pair(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
) -> dict:
    """Quick 'are these two clips the same footage?' check."""
    paths = []
    for upload, tag in ((file_a, "a"), (file_b, "b")):
        path = config.TMP_DIR / f"{uuid.uuid4().hex}{Path(upload.filename or '').suffix or '.mp4'}"
        with open(path, "wb") as out:
            while chunk := await upload.read(1024 * 1024):
                out.write(chunk)
        paths.append(path)
    try:
        results = []
        for path in paths:
            extracted = await asyncio.to_thread(media.extract_file, path, 8)
            results.append(extracted.fingerprint())
        from .dedup import compare as cmp_fp

        comparison = cmp_fp(results[0], results[1])
        return {"comparison": comparison.to_dict()}
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
class SearchRequest(BaseModel):
    providers: list[str] | None = None
    depth: int = 1


async def _search_job(job_id: str, clip_id: int, body: SearchRequest) -> None:
    settings = db.get_settings()
    clip = db.get_clip(clip_id, with_candidates=False)
    if not clip:
        job_update(job_id, status="error", error="clip not found")
        return
    frame_dir = config.CLIPS_DIR / str(clip_id) / "frames"
    frame_files = sorted(frame_dir.glob("*.jpg"))[:6] if frame_dir.exists() else []
    if not frame_files:
        job_update(job_id, status="error", error="no frames stored for this clip")
        return
    images = [f.read_bytes() for f in frame_files]
    meta = {
        "width": clip.get("width") or 0,
        "height": clip.get("height") or 0,
        "bing_key": settings.get("bing_key", ""),
    }
    try:
        job_update(job_id, message="asking the search engines…", progress=35, stage="search")
        result = await run_search(clip_id, images, meta, settings, body.providers, body.depth)
        job_update(job_id, status="done", stage="done", progress=100,
                   message=f"{len(result.get('candidates', []))} links found", result=result)
    except Exception as exc:  # noqa: BLE001
        job_update(job_id, status="error", error=f"{type(exc).__name__}: {exc}",
                   message="search failed")


@app.post("/api/clips/{clip_id}/search")
async def search_clip(clip_id: int, body: SearchRequest) -> dict:
    if not db.get_clip(clip_id, with_candidates=False):
        raise HTTPException(404, "clip not found")
    job_id = new_job("search", clip_id=clip_id)
    spawn(_search_job(job_id, clip_id, body))
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = job_get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/api/jobs")
async def list_jobs() -> dict:
    with _job_lock:
        jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)[:30]
    return {"jobs": [{k: v for k, v in j.items() if k != "result"} for j in jobs]}


# --------------------------------------------------------------------------- #
# clips
# --------------------------------------------------------------------------- #
@app.get("/api/clips")
async def clips(q: str = "", status: str = "", cluster_id: int | None = None) -> dict:
    return {"clips": db.list_clips(q, status, cluster_id), "stats": db.stats()}


@app.get("/api/clips/{clip_id}")
async def clip_detail(clip_id: int) -> dict:
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "clip not found")
    return {"clip": clip}


class ClipPatch(BaseModel):
    title: str | None = None
    notes: str | None = None
    used_in: str | None = None
    tags: list[str] | None = None
    times_used: int | None = None


@app.patch("/api/clips/{clip_id}")
async def patch_clip(clip_id: int, patch: ClipPatch) -> dict:
    values = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not db.get_clip(clip_id, with_candidates=False):
        raise HTTPException(404, "clip not found")
    return {"clip": db.update_clip(clip_id, values)}


@app.post("/api/clips/{clip_id}/use")
async def use_clip(clip_id: int, body: dict | None = None) -> dict:
    body = body or {}
    clip = db.mark_used(clip_id, body.get("used_in", ""), body.get("times"))
    if not clip:
        raise HTTPException(404, "clip not found")
    return {"clip": clip}


@app.delete("/api/clips/{clip_id}")
async def remove_clip(clip_id: int) -> dict:
    db.delete_clip(clip_id)
    folder = config.CLIPS_DIR / str(clip_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    return {"ok": True}


@app.post("/api/clips/{clip_id}/candidates")
async def add_manual_candidate(clip_id: int, body: dict) -> dict:
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    if not url.startswith("http"):
        url = "https://" + url
    if not is_interesting(url):
        raise HTTPException(400, "that does not look like a page URL")
    canonical = canonicalize(url)
    kind, label = classify(canonical)
    title = str(body.get("title") or "").strip()
    author = str(body.get("author") or "").strip()
    thumb = str(body.get("thumb") or "").strip()
    if not title:
        try:
            from .search.base import make_client
            from .search.enrich import enrich_one

            async with make_client(timeout=8) as client:
                info = await enrich_one(client, canonical)
            title = info.get("title", "")
            author = author or info.get("author", "")
            thumb = thumb or info.get("thumb", "")
        except Exception:
            pass
    candidate = db.add_candidate(
        clip_id, provider="manual", url=canonical, canonical=canonical, host=host_of(canonical),
        title=title, author=author, kind=kind, thumb=thumb, score=90,
        reasons=["added by hand"], manual=True, confirmed=True,
    )
    db.update_clip(clip_id, {"best_source": canonical, "status": "sourced"})
    return {"candidate": candidate, "clip": db.get_clip(clip_id)}


@app.post("/api/candidates/{candidate_id}/confirm")
async def confirm_candidate(candidate_id: int, body: dict | None = None) -> dict:
    body = body or {}
    confirmed = bool(body.get("confirmed", True))
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise HTTPException(404, "candidate not found")
        clip_id = row["clip_id"]
        url = row["canonical"] or row["url"]
        conn.execute("UPDATE candidates SET confirmed=? WHERE id=?", (1 if confirmed else 0, candidate_id))
        if confirmed:
            conn.execute("UPDATE candidates SET confirmed=0 WHERE clip_id=? AND id<>?", (clip_id, candidate_id))
    db.update_clip(clip_id, {"best_source": url if confirmed else "", "status": "sourced"})
    return {"clip": db.get_clip(clip_id)}


@app.delete("/api/candidates/{candidate_id}")
async def delete_candidate(candidate_id: int) -> dict:
    with db.tx() as conn:
        row = conn.execute("SELECT clip_id FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise HTTPException(404, "candidate not found")
        clip_id = row["clip_id"]
        conn.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
    return {"clip": db.get_clip(clip_id)}


# --------------------------------------------------------------------------- #
# duplicates + folder scan
# --------------------------------------------------------------------------- #
@app.get("/api/duplicates")
async def duplicates() -> dict:
    return {"groups": db.clusters(), "stats": db.stats()}


@app.post("/api/clusters/{clip_id}/unlink")
async def unlink_cluster(clip_id: int) -> dict:
    db.update_clip(clip_id, {"cluster_id": None, "verdict": "", "match_score": 0})
    return {"ok": True}


class ScanRequest(BaseModel):
    folder: str
    recursive: bool = True
    limit: int = 400


async def _scan_job(job_id: str, body: ScanRequest) -> None:
    settings = db.get_settings()
    folder = Path(body.folder).expanduser()
    if not folder.exists() or not folder.is_dir():
        job_update(job_id, status="error", error=f"folder not found: {folder}")
        return

    exts = media.VIDEO_EXT | media.IMAGE_EXT
    files: list[Path] = []
    iterator = folder.rglob("*") if body.recursive else folder.glob("*")
    for path in iterator:
        if path.is_file() and path.suffix.lower() in exts:
            files.append(path)
        if len(files) >= max(1, min(body.limit, 2000)):
            break

    added, repeats, errors = [], [], []
    total = max(1, len(files))
    for idx, path in enumerate(files):
        job_update(job_id, progress=int(idx / total * 100),
                   message=f"{idx + 1}/{len(files)} · {path.name}")
        try:
            extracted = await asyncio.to_thread(media.extract_file, path, int(settings.get("frames", 6)))
            fingerprint = extracted.fingerprint()
            matches = await asyncio.to_thread(db.similar_clips, fingerprint, None, settings)
            top = matches[0] if matches else None
            is_dup = bool(top and top["comparison"]["verdict"] == "duplicate")
            clip = db.create_clip(
                title=path.stem[:120],
                source_label=path.name,
                source_kind=extracted.kind,
                fingerprint=fingerprint,
                thumb="",
                duration=extracted.duration,
                width=extracted.width,
                height=extracted.height,
                cluster_id=(top.get("cluster_id") or top["id"]) if is_dup else None,
            )
            clip_dir = config.CLIPS_DIR / str(clip["id"])
            if extracted.frames:
                names = await asyncio.to_thread(media.save_frames, extracted.frames, clip_dir / "frames")
                mid = extracted.frames[len(extracted.frames) // 2]
                from PIL import Image

                with Image.open(io.BytesIO(mid.data)) as im:
                    im = im.convert("RGB")
                    w = min(480, im.width)
                    im.resize((w, max(2, int(im.height * w / im.width)))).save(
                        clip_dir / "thumb.jpg", "JPEG", quality=86)
                db.update_clip(clip["id"], {
                    "thumb": f"{clip['id']}/thumb.jpg",
                    "status": "duplicate" if is_dup else "analyzed",
                    "verdict": top["comparison"]["verdict"] if top else "",
                    "match_score": top["comparison"]["score"] if top else 0,
                })
            row = {
                "id": clip["id"],
                "title": path.stem,
                "file": str(path),
                "thumb": f"{clip['id']}/thumb.jpg",
                "match": top,
            }
            (repeats if is_dup else added).append(row)
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": str(path), "error": str(exc)[:200]})

    job_update(job_id, status="done", progress=100, stage="done",
               message=f"{len(added)} new · {len(repeats)} repeats",
               result={"added": added, "repeats": repeats, "errors": errors,
                       "scanned": len(files), "folder": str(folder)})


@app.post("/api/scan")
async def scan_folder(body: ScanRequest) -> dict:
    job_id = new_job("scan")
    spawn(_scan_job(job_id, body))
    return {"job_id": job_id}


# --------------------------------------------------------------------------- #
# export / misc
# --------------------------------------------------------------------------- #
@app.get("/api/export.csv")
async def export_csv() -> StreamingResponse:
    rows = []
    for clip in db.list_clips(limit=5000):
        detail = db.get_clip(clip["id"])
        best = (detail or {}).get("candidates", [])
        source = next((c for c in best if c["confirmed"]), None) or (best[0] if best else None)
        rows.append([
            clip["id"], clip["title"], clip["status"], clip["times_used"], clip["used_in"],
            clip["tags"] if isinstance(clip["tags"], str) else ",".join(clip["tags"]),
            (source or {}).get("url", ""),
            (source or {}).get("title", ""),
            (source or {}).get("author", ""),
            clip["notes"], clip["created_at"],
        ])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "title", "status", "times_used", "used_in", "tags",
                     "best_source_url", "best_source_title", "best_source_channel", "notes", "added"])
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sourcerers-library.csv"},
    )


@app.get("/api/clips/{clip_id}/open-in-browser")
async def lens_help(clip_id: int) -> dict:
    """Deep links for the manual fallback."""
    clip = db.get_clip(clip_id, with_candidates=False)
    if not clip:
        raise HTTPException(404, "clip not found")
    return {
        "lens": "https://lens.google.com/",
        "yandex": "https://yandex.com/images/",
        "tineye": "https://tineye.com/",
        "frame": f"/clips/{clip_id}/frames/frame_00.jpg",
    }


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": config.APP_VERSION}


def serve() -> None:  # pragma: no cover - entry point
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
