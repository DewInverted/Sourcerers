from __future__ import annotations

from PIL import Image, ImageDraw

from app.hashing import (
    Fingerprint,
    color_hist,
    dhash,
    frame_distance,
    hash_image,
    hamming,
    hist_distance,
    phash,
)


def make_image(seed: int = 0, size=(320, 180)) -> Image.Image:
    img = Image.new("RGB", size, (20 + seed * 7 % 200, 40, 90))
    draw = ImageDraw.Draw(img)
    for i in range(6):
        x = (i * 37 + seed * 23) % size[0]
        y = (i * 19 + seed * 11) % size[1]
        draw.rectangle([x, y, x + 60, y + 40], fill=(200 - i * 20, 120, 60 + seed * 5 % 150))
    return img


def test_hashes_are_stable_and_sized():
    img = make_image(1)
    assert 0 <= dhash(img) < 2**64
    assert 0 <= phash(img) < 2**64
    assert abs(sum(color_hist(img)) - 1.0) < 1e-6


def test_same_frame_identical():
    img = make_image(3)
    a, b = hash_image(img), hash_image(img.copy())
    assert frame_distance(a, b) == 0


def test_resize_is_tolerated():
    img = make_image(4)
    small = img.resize((160, 90))
    assert frame_distance(hash_image(img), hash_image(small)) < 0.08


def test_different_frames_differ():
    assert frame_distance(hash_image(make_image(1)), hash_image(make_image(9))) > 0.25


def test_recompression_is_tolerated(tmp_path):
    from io import BytesIO

    img = make_image(5)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=35)
    buf.seek(0)
    again = Image.open(buf).convert("RGB")
    assert frame_distance(hash_image(img), hash_image(again)) < 0.08


def test_hamming_and_hist_distance():
    assert hamming(0, 0) == 0
    assert hamming(0b1010, 0b0101) == 4
    assert hist_distance([1.0, 0.0], [1.0, 0.0]) == 0
    assert hist_distance([1.0, 0.0], [0.0, 1.0]) == 1


def test_fingerprint_roundtrip():
    fp = Fingerprint(frames=[hash_image(make_image(2))], duration=3.5, width=320, height=180)
    clone = Fingerprint.from_json(fp.to_json())
    assert clone.duration == 3.5
    assert len(clone.frames) == 1
    assert frame_distance(fp.frames[0], clone.frames[0]) == 0
