"""Configuration and paths for Sourcerers.

Everything lives inside the repo's ``data/`` folder by default so the app is
fully portable (copy the folder, keep your library).  Override with the
``SOURCERERS_DATA`` environment variable if you want it elsewhere.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent


def _data_dir() -> Path:
    env = os.environ.get("SOURCERERS_DATA")
    base = Path(env).expanduser().resolve() if env else (ROOT_DIR / "data")
    base.mkdir(parents=True, exist_ok=True)
    (base / "clips").mkdir(exist_ok=True)
    (base / "tmp").mkdir(exist_ok=True)
    return base


DATA_DIR = _data_dir()
CLIPS_DIR = DATA_DIR / "clips"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "sourcerers.db"
STATIC_DIR = APP_DIR / "static"

APP_NAME = "Sourcerers"
APP_VERSION = "1.0.0"

# How many frames we sample out of a clip when nobody says otherwise.
DEFAULT_FRAME_COUNT = 6
MAX_FRAME_COUNT = 24

# Frame thumbnail width used for previews / UI.
THUMB_WIDTH = 480
# Width we downscale to before hashing (keeps hashing fast + comparable).
HASH_WIDTH = 256

# --- duplicate detection defaults -------------------------------------------
# Mean normalised frame distance below which two clips are "the same footage".
DUP_MEAN_DISTANCE = 0.20
# Fraction of a clip's frames that must find a partner before we call it a dup.
DUP_COVERAGE = 0.45
# Looser band -> "similar, double check" instead of "different".
NEAR_MEAN_DISTANCE = 0.28
NEAR_COVERAGE = 0.25

# --- search defaults --------------------------------------------------------
DEFAULT_PROVIDERS = ("lens", "yandex")
SEARCH_TIMEOUT = 30.0          # seconds per provider
ENRICH_TIMEOUT = 6.0           # seconds per metadata lookup
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SETTING_DEFAULTS: dict[str, object] = {
    "frames": DEFAULT_FRAME_COUNT,
    "dup_mean_distance": DUP_MEAN_DISTANCE,
    "dup_coverage": DUP_COVERAGE,
    "providers": list(DEFAULT_PROVIDERS),
    "bing_key": "",
    "search_timeout": SEARCH_TIMEOUT,
    "enrich_results": True,
    "prefer_full_video": True,
    "demo_mode": False,
    "open_browser": True,
}


def find_ffmpeg() -> str | None:
    """Locate an ffmpeg binary: system install first, then the pip wheel."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # imageio-ffmpeg ships a static build, so video support is free
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_ytdlp() -> str | None:
    """yt-dlp is optional (only needed to pull a clip straight from a URL)."""
    exe = shutil.which("yt-dlp")
    if exe:
        return exe
    try:
        import yt_dlp  # noqa: F401

        return "yt-dlp-module"
    except Exception:
        return None


FFMPEG = find_ffmpeg()
YTDLP = find_ytdlp()
