"""Perceptual fingerprinting.

No numpy / opencv needed: dHash + pHash + a small colour histogram, all done
with Pillow.  Fast enough to fingerprint a clip in milliseconds and stable
across re-encodes, resizes and mild colour grading.

A ``FrameHash`` captures one sampled frame.  A clip's fingerprint is the list
of its frame hashes plus an optional duration, which is all dedup needs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from PIL import Image, ImageOps

from .config import HASH_WIDTH

DCT_SIZE = 32
DHASH_BITS = 64
PHASH_BITS = 64


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def _pixels(img: Image.Image) -> list:
    """Pillow renamed getdata() -> get_flattened_data(); support both."""
    getter = getattr(img, "get_flattened_data", None) or img.getdata
    return list(getter())


def _prepare(img: Image.Image) -> Image.Image:
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


def dhash(img: Image.Image, size: int = 8) -> int:
    """Difference hash: 8x8 neighbouring-pixel comparisons -> 64 bits."""
    g = _prepare(img).convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = _pixels(g)
    bits = 0
    idx = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            if px[base + col] > px[base + col + 1]:
                bits |= 1 << idx
            idx += 1
    return bits


def _dct_1d(values: Sequence[float]) -> list[float]:
    n = len(values)
    out = [0.0] * n
    factor = math.pi / (2 * n)
    for k in range(n):
        s = 0.0
        ck = math.cos(factor * k)
        for i, v in enumerate(values):
            s += v * math.cos(factor * (2 * i + 1) * k)
        out[k] = s * (math.sqrt(1 / n) if k == 0 else math.sqrt(2 / n))
    return out


def phash(img: Image.Image, size: int = DCT_SIZE, keep: int = 8) -> int:
    """DCT hash: low-frequency energy of a 32x32 greyscale image -> 64 bits."""
    g = _prepare(img).convert("L").resize((size, size), Image.Resampling.LANCZOS)
    px = _pixels(g)
    rows = [_dct_1d(px[r * size:(r + 1) * size]) for r in range(size)]
    # transpose then DCT the columns (separable 2-D DCT)
    cols = [_dct_1d([rows[r][c] for r in range(size)]) for c in range(size)]
    low = []
    for r in range(keep):
        for c in range(keep):
            if r == 0 and c == 0:
                continue
            low.append(cols[c][r])
    if not low:
        return 0
    ordered = sorted(low)
    median = ordered[len(ordered) // 2]
    bits = 0
    for i, v in enumerate(low):
        if v > median:
            bits |= 1 << i
    return bits


def color_hist(img: Image.Image, bins: int = 4) -> list[float]:
    """Normalised 4x4x4 RGB histogram (64 floats, sums to 1)."""
    small = _prepare(img).convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
    hist = [0.0] * (bins**3)
    step = 256 // bins
    for r, g, b in _pixels(small):
        i = (r // step) * bins * bins + (g // step) * bins + (b // step)
        hist[i] += 1.0
    total = sum(hist) or 1.0
    return [v / total for v in hist]


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def hist_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Half the L1 distance between two normalised histograms -> 0..1."""
    if not a or not b:
        return 0.5
    return 0.5 * sum(abs(x - y) for x, y in zip(a, b))


# --------------------------------------------------------------------------- #
# frame / clip fingerprints
# --------------------------------------------------------------------------- #
@dataclass
class FrameHash:
    d: int
    p: int
    h: list[float] = field(default_factory=list)
    t: float = 0.0

    def to_json(self) -> dict:
        return {"d": f"{self.d:016x}", "p": f"{self.p:016x}", "h": self.h, "t": round(self.t, 2)}

    @classmethod
    def from_json(cls, raw: dict) -> "FrameHash":
        return cls(
            d=int(raw.get("d", "0"), 16),
            p=int(raw.get("p", "0"), 16),
            h=list(raw.get("h") or []),
            t=float(raw.get("t", 0) or 0),
        )


@dataclass
class Fingerprint:
    frames: list[FrameHash]
    duration: float = 0.0
    width: int = 0
    height: int = 0

    def to_json(self) -> dict:
        return {
            "frames": [f.to_json() for f in self.frames],
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_json(cls, raw: dict | None) -> "Fingerprint":
        if not raw:
            return cls(frames=[])
        return cls(
            frames=[FrameHash.from_json(f) for f in raw.get("frames", [])],
            duration=float(raw.get("duration", 0) or 0),
            width=int(raw.get("width", 0) or 0),
            height=int(raw.get("height", 0) or 0),
        )


def hash_image(img: Image.Image, at: float = 0.0) -> FrameHash:
    img = _prepare(img)
    small = img
    if img.width > HASH_WIDTH:
        ratio = HASH_WIDTH / float(img.width)
        small = img.resize((HASH_WIDTH, max(2, int(img.height * ratio))), Image.Resampling.LANCZOS)
    return FrameHash(d=dhash(small), p=phash(small), h=color_hist(small), t=at)


def hash_bytes(data: bytes) -> FrameHash:
    from io import BytesIO

    with Image.open(BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        return hash_image(im)


def frame_distance(a: FrameHash, b: FrameHash) -> float:
    """0 = identical frame, 1 = nothing alike.

    dHash is weighted highest because it survives crops/overlays best, pHash
    adds structural detail, the histogram is only a tie-breaker so colour
    grading does not blow the score up.
    """
    dh = hamming(a.d, b.d) / DHASH_BITS
    ph = hamming(a.p, b.p) / PHASH_BITS
    hh = hist_distance(a.h, b.h)
    return 0.55 * dh + 0.30 * ph + 0.15 * hh


def frame_matrix(a: Fingerprint, b: Fingerprint) -> list[list[float]]:
    return [[frame_distance(fa, fb) for fb in b.frames] for fa in a.frames]


def mean(values: Iterable[float]) -> float:
    vals = [v for v in values]
    return sum(vals) / len(vals) if vals else 1.0
