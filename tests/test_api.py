from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import app
from app import db


@pytest.fixture(scope="module")
def client():
    db.init()
    with TestClient(app) as c:
        yield c


def png_bytes(seed: int = 0, size=(320, 180)) -> bytes:
    img = Image.new("RGB", size, (30, 40, 90))
    draw = ImageDraw.Draw(img)
    for i in range(5):
        x = (i * 47 + seed * 31) % size[0]
        y = (i * 29 + seed * 17) % size[1]
        draw.ellipse([x, y, x + 70, y + 50], fill=(220 - i * 20, 130 + i * 5, 50 + seed * 11 % 180))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def wait_job(client, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} timed out")


def analyze(client, data: bytes, name="shot.png") -> dict:
    res = client.post("/api/analyze", files={"file": (name, data, "image/png")})
    assert res.status_code == 200, res.text
    return wait_job(client, res.json()["job_id"])


def test_state(client):
    state = client.get("/api/state").json()
    assert state["app"] == "Sourcerers"
    assert {p["id"] for p in state["providers"]} >= {"lens", "yandex", "bing", "demo"}
    assert "ffmpeg" in state["capabilities"]


def test_settings_roundtrip(client):
    res = client.post("/api/settings", json={"frames": 9, "dup_coverage": 0.5})
    assert res.json()["settings"]["frames"] == 9
    # out-of-range values are clamped
    res = client.post("/api/settings", json={"frames": 999})
    assert res.json()["settings"]["frames"] <= 24
    client.post("/api/settings", json={"frames": 6, "dup_coverage": 0.45})


def test_index_and_static(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_analyze_image_and_dedupe(client):
    first = analyze(client, png_bytes(1))
    assert first["status"] == "done"
    clip = first["result"]["clip"]
    assert first["result"]["dedup"]["verdict"] == "distinct"
    assert clip["thumb"]
    assert client.get(f"/clips/{clip['thumb']}").status_code == 200

    # same picture again -> duplicate, no new search needed
    second = analyze(client, png_bytes(1))
    dedup = second["result"]["dedup"]
    assert dedup["verdict"] == "duplicate"
    assert dedup["match"]["id"] == clip["id"]
    assert second["result"]["clip"]["status"] == "duplicate"

    # a different picture -> not a duplicate
    third = analyze(client, png_bytes(9))
    assert third["result"]["dedup"]["verdict"] != "duplicate"


def test_manual_source_confirm_and_export(client):
    clip = analyze(client, png_bytes(3))["result"]["clip"]
    res = client.post(f"/api/clips/{clip['id']}/candidates",
                      json={"url": "www.youtube.com/watch?v=dQw4w9WgXcQ", "title": "The original"})
    cand = res.json()["candidate"]
    assert cand["confirmed"] is True
    assert cand["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    detail = client.get(f"/api/clips/{clip['id']}").json()["clip"]
    assert detail["best_source"].startswith("https://www.youtube.com/watch")

    # un-confirm works too
    client.post(f"/api/candidates/{cand['id']}/confirm", json={"confirmed": False})
    assert client.get(f"/api/clips/{clip['id']}").json()["clip"]["best_source"] == ""

    client.patch(f"/api/clips/{clip['id']}", json={"used_in": "Ranking #12", "times_used": 3})
    csv = client.get("/api/export.csv")
    assert csv.status_code == 200
    assert "best_source_url" in csv.text
    assert "Ranking #12" in csv.text


def test_demo_search_flow(client):
    client.post("/api/settings", json={"demo_mode": True})
    try:
        clip = analyze(client, png_bytes(5))["result"]["clip"]
        res = client.post(f"/api/clips/{clip['id']}/search", json={"providers": ["demo"], "depth": 1})
        job = wait_job(client, res.json()["job_id"])
        assert job["status"] == "done", job
        cands = job["result"]["candidates"]
        assert cands
        assert cands[0]["score"] >= cands[-1]["score"]
        detail = client.get(f"/api/clips/{clip['id']}").json()["clip"]
        assert detail["status"] == "sourced"
        assert detail["best_source"]

        dupes = client.get("/api/duplicates").json()
        assert "groups" in dupes
    finally:
        client.post("/api/settings", json={"demo_mode": False})


def test_delete_clip(client):
    clip = analyze(client, png_bytes(11))["result"]["clip"]
    assert client.delete(f"/api/clips/{clip['id']}").status_code == 200
    assert client.get(f"/api/clips/{clip['id']}").status_code == 404


def test_analyze_requires_input(client):
    assert client.post("/api/analyze", data={}).status_code == 400


def test_bad_video_reports_error(client):
    res = client.post("/api/analyze", files={"file": ("broken.mp4", b"not a video", "video/mp4")})
    job = wait_job(client, res.json()["job_id"])
    assert job["status"] == "error"
    assert "frame" in job["error"].lower() or "corrupt" in job["error"].lower()


def test_upload_keeps_filename_as_title(client):
    job = client.post("/api/analyze",
                      files={"file": ("Best movie scene ever.mp4".replace(".mp4", ".png"),
                                      png_bytes(13), "image/png")})
    result = wait_job(client, job.json()["job_id"])["result"]["clip"]
    assert result["title"] == "Best movie scene ever"
    assert result["source_label"] == "Best movie scene ever.png"
