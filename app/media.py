"""Media in, frames out.

Uses ffmpeg (system install, or the static binary that comes with the
``imageio-ffmpeg`` pip package) to sample frames from a video.  Stills and GIFs
go straight through Pillow.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from . import config
from .hashing import Fingerprint, hash_image

VIDEO_EXT = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".heic"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}


class MediaError(RuntimeError):
    pass


@dataclass
class ExtractedFrame:
    index: int
    time: float
    data: bytes            # JPEG bytes (downscaled)
    width: int
    height: int


@dataclass
class ExtractResult:
    frames: list[ExtractedFrame]
    duration: float
    width: int
    height: int
    kind: str              # "video" | "image"

    def fingerprint(self) -> Fingerprint:
        hashes = []
        for f in self.frames:
            with Image.open(BytesIO(f.data)) as im:
                hashes.append(hash_image(im, at=f.time))
        return Fingerprint(frames=hashes, duration=self.duration, width=self.width, height=self.height)


# --------------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------------- #
def ffmpeg_exe() -> str:
    exe = config.FFMPEG
    if not exe:
        raise MediaError(
            "ffmpeg was not found. Install it (brew install ffmpeg / winget install ffmpeg) "
            "or run: pip install imageio-ffmpeg"
        )
    return exe


def probe(path: Path) -> dict:
    """Best-effort duration/dimensions. Tries ffprobe, falls back to ffmpeg -i."""
    exe = config.FFMPEG
    if not exe:
        return {}
    info: dict = {}
    probe_exe = shutil.which("ffprobe")
    if probe_exe:
        try:
            out = subprocess.run(
                [probe_exe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                capture_output=True, timeout=30,
            ).stdout
            data = json.loads(out or b"{}")
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    info["width"] = int(stream.get("width") or 0)
                    info["height"] = int(stream.get("height") or 0)
                    dur = stream.get("duration") or data.get("format", {}).get("duration")
                    if dur:
                        info["duration"] = float(dur)
                    break
            return info
        except Exception:
            pass
    try:
        res = subprocess.run([exe, "-i", str(path)], capture_output=True, timeout=30)
        text = res.stderr.decode("utf-8", "ignore")
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", text)
        if m:
            info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
        if m:
            info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return info


def _frame_to_jpeg(raw: bytes, max_width: int = 720) -> tuple[bytes, int, int]:
    with Image.open(BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        if im.width > max_width:
            ratio = max_width / float(im.width)
            im = im.resize((max_width, max(2, int(im.height * ratio))), Image.Resampling.LANCZOS)
        buf = BytesIO()
        im.save(buf, "JPEG", quality=88)
        return buf.getvalue(), im.width, im.height


def extract_image(data: bytes) -> ExtractResult:
    with Image.open(BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if getattr(im, "n_frames", 1) > 1:  # animated gif / webp
            im.seek(0)
        im = im.convert("RGB")
        w, h = im.size
        if im.width > 720:
            ratio = 720 / float(im.width)
            im = im.resize((720, max(2, int(im.height * ratio))), Image.Resampling.LANCZOS)
        buf = BytesIO()
        im.save(buf, "JPEG", quality=88)
        frame = ExtractedFrame(0, 0.0, buf.getvalue(), im.width, im.height)
    return ExtractResult(frames=[frame], duration=0.0, width=w, height=h, kind="image")


def extract_video(path: Path, count: int = config.DEFAULT_FRAME_COUNT,
                  start: float | None = None, end: float | None = None) -> ExtractResult:
    exe = ffmpeg_exe()
    info = probe(path)
    duration = float(info.get("duration") or 0.0)
    limit_start = start or 0.0
    usable = duration - limit_start if duration else 0.0
    if end is not None and end > limit_start:
        usable = min(usable, end - limit_start) if usable else (end - limit_start)
    count = max(1, min(int(count), config.MAX_FRAME_COUNT))

    if usable and usable > 0:
        # skip the very first/last moments: intros and fades are bad fingerprints
        pad = min(usable * 0.06, 1.5)
        span_start = limit_start + pad
        span_end = limit_start + usable - pad
        if span_end <= span_start:
            span_start, span_end = limit_start, limit_start + max(usable, 0.1)
        if count == 1:
            times = [(span_start + span_end) / 2]
        else:
            step = (span_end - span_start) / (count - 1)
            times = [span_start + step * i for i in range(count)]
    else:
        times = [0.0] if count == 1 else [i * 1.5 for i in range(count)]

    frames: list[ExtractedFrame] = []
    for idx, t in enumerate(times):
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(path),
               "-frames:v", "1", "-vf", "scale='min(720,iw)':-2", "-f", "image2",
               "-vcodec", "mjpeg", "-q:v", "3", "-"]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=45)
        except subprocess.TimeoutExpired:
            continue
        if res.returncode != 0 or not res.stdout:
            continue
        try:
            data, w, h = _frame_to_jpeg(res.stdout)
        except Exception:
            continue
        frames.append(ExtractedFrame(idx, round(t, 2), data, w, h))

    if not frames:
        raise MediaError("could not read any frames from this video (is the file corrupt?)")

    return ExtractResult(
        frames=frames,
        duration=duration,
        width=int(info.get("width") or frames[0].width),
        height=int(info.get("height") or frames[0].height),
        kind="video",
    )


def extract_file(path: Path, count: int = config.DEFAULT_FRAME_COUNT) -> ExtractResult:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXT:
        return extract_image(path.read_bytes())
    if suffix in VIDEO_EXT:
        return extract_video(path, count=count)
    if suffix in AUDIO_EXT:
        raise MediaError("that is an audio file — there is nothing to reverse-search")
    # sniff
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n":
        return extract_image(path.read_bytes())
    return extract_video(path, count=count)


def save_frames(frames: list[ExtractedFrame], out_dir: Path, prefix: str = "frame") -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for f in frames:
        name = f"{prefix}_{f.index:02d}.jpg"
        (out_dir / name).write_bytes(f.data)
        names.append(name)
    return names


def download_video(url: str, dest: Path, max_seconds: int = 300) -> Path:
    """Grab a clip from a URL with yt-dlp (optional dependency)."""
    if not config.YTDLP:
        raise MediaError(
            "yt-dlp is not installed, so I cannot fetch a video from a link. "
            "Run: pip install yt-dlp  (or download the clip and drop the file in instead)"
        )
    fmt = "b[ext=mp4][height<=480]/b[height<=720]/bv*[height<=720]+ba/b"
    ffmpeg_dir = str(Path(config.FFMPEG).parent) if config.FFMPEG else ""
    cmd = [
        "yt-dlp", "-q", "--no-warnings", "--no-playlist",
        "-f", fmt, "--merge-output-format", "mp4",
        "--download-sections", f"*0-{max_seconds}",
        "-o", str(dest), url,
    ]
    if ffmpeg_dir:
        cmd[1:1] = ["--ffmpeg-location", ffmpeg_dir]
    if config.YTDLP == "yt-dlp-module":
        try:
            import yt_dlp

            opts = {
                "quiet": True, "no_warnings": True, "noplaylist": True,
                "format": fmt, "merge_output_format": "mp4",
                **({"ffmpeg_location": str(Path(config.FFMPEG).parent)} if config.FFMPEG else {}),
                "download_ranges": yt_dlp.utils.download_range_func(None, [(0, max_seconds)]),
                "outtmpl": str(dest),
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:  # pragma: no cover - depends on network
            raise MediaError(f"download failed: {exc}") from exc
    else:
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise MediaError("download timed out after 5 minutes") from exc
        if res.returncode != 0:
            msg = res.stderr.decode("utf-8", "ignore").strip().splitlines()
            raise MediaError("download failed: " + (msg[-1] if msg else "unknown error"))

    candidates = list(dest.parent.glob(dest.stem + "*"))
    if not candidates:
        raise MediaError("download finished but no file was produced")
    return max(candidates, key=lambda p: p.stat().st_size)
