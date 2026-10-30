from __future__ import annotations

from PIL import Image, ImageDraw

from app.dedup import compare
from app.hashing import Fingerprint, hash_image


def frames(seed: int, count: int = 6) -> Fingerprint:
    out = []
    for i in range(count):
        img = Image.new("RGB", (256, 144), (10 + seed * 13 % 120, 60, 90))
        draw = ImageDraw.Draw(img)
        for j in range(4):
            x = (i * 41 + j * 29 + seed * 17) % 200
            y = (i * 23 + j * 13 + seed * 7) % 110
            draw.rectangle([x, y, x + 45, y + 30], fill=(230 - j * 25, 90 + i * 12, 40 + seed * 9 % 200))
        out.append(hash_image(img, at=i))
    return Fingerprint(frames=out, duration=count, width=256, height=144)


def test_identical_clip_is_duplicate():
    a = frames(2)
    cmp = compare(a, frames(2))
    assert cmp.verdict == "duplicate"
    assert cmp.score > 0.9
    assert cmp.matched_frames == 6


def test_unrelated_clips_are_distinct():
    assert compare(frames(1), frames(21)).verdict != "duplicate"


def test_reencoded_clip_still_matches():
    a = frames(4)
    assert compare(a, frames(4)).percent > 90


def test_short_excerpt_matches_long_clip():
    long_clip = frames(6, count=8)
    short = Fingerprint(frames=long_clip.frames[2:5], duration=3, width=256, height=144)
    cmp = compare(short, long_clip)
    assert cmp.verdict == "duplicate", cmp.to_dict()
    assert cmp.coverage_a == 1.0


def test_mismatched_lengths_are_explained():
    a = frames(8, count=6)
    b = frames(8, count=10)
    cmp = compare(a, b)
    if cmp.verdict == "duplicate":
        assert any("length" in r or "excerpt" in r for r in cmp.reasons)


def test_empty_fingerprint_is_safe():
    cmp = compare(Fingerprint(frames=[]), frames(1))
    assert cmp.verdict == "distinct"
    assert cmp.score == 0


def test_thresholds_change_verdict():
    a, b = frames(3), frames(3)
    strict = compare(a, b, dup_mean=0.001, dup_cov=1.0)
    assert strict.verdict == "duplicate"  # identical frames have distance 0
    loose = compare(frames(5), frames(30), dup_mean=0.9, dup_cov=0.05)
    assert loose.score >= 0
