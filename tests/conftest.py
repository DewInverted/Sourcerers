"""Test config: keep every test's data in a throwaway folder."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="sourcerers-tests-")
os.environ["SOURCERERS_DATA"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return Path(_TMP)


@pytest.fixture(scope="session")
def sample_video(data_dir):
    """A tiny generated clip, or skip if ffmpeg is unavailable."""
    from app import config

    if not config.FFMPEG:
        pytest.skip("ffmpeg not available")
    import subprocess

    out = data_dir / "sample.mp4"
    subprocess.run(
        [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=4",
         "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )
    return out
