"""SQLite storage: clips, their fingerprints, and every source we found."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config
from .dedup import compare
from .hashing import Fingerprint

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL DEFAULT '',
    source_label    TEXT    NOT NULL DEFAULT '',
    source_kind     TEXT    NOT NULL DEFAULT 'video',
    duration        REAL    NOT NULL DEFAULT 0,
    width           INTEGER NOT NULL DEFAULT 0,
    height          INTEGER NOT NULL DEFAULT 0,
    frames          INTEGER NOT NULL DEFAULT 0,
    fingerprint     TEXT    NOT NULL DEFAULT '{}',
    thumb           TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'new',
    verdict         TEXT    NOT NULL DEFAULT '',
    match_score     REAL    NOT NULL DEFAULT 0,
    cluster_id      INTEGER,
    times_used      INTEGER NOT NULL DEFAULT 0,
    used_in         TEXT    NOT NULL DEFAULT '',
    notes           TEXT    NOT NULL DEFAULT '',
    tags            TEXT    NOT NULL DEFAULT '',
    best_source     TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    last_searched   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id      INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    provider     TEXT    NOT NULL DEFAULT '',
    url          TEXT    NOT NULL,
    canonical    TEXT    NOT NULL DEFAULT '',
    host         TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL DEFAULT '',
    author       TEXT    NOT NULL DEFAULT '',
    published    TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT 'page',
    thumb        TEXT    NOT NULL DEFAULT '',
    score        REAL    NOT NULL DEFAULT 0,
    reasons      TEXT    NOT NULL DEFAULT '[]',
    mentions     INTEGER NOT NULL DEFAULT 1,
    manual       INTEGER NOT NULL DEFAULT 0,
    confirmed    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS search_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id     INTEGER NOT NULL,
    provider    TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'running',
    found       INTEGER NOT NULL DEFAULT 0,
    ms          INTEGER NOT NULL DEFAULT 0,
    error       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_clip ON candidates(clip_id);
CREATE INDEX IF NOT EXISTS idx_clips_cluster ON clips(cluster_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    with _write_lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def get_settings() -> dict[str, Any]:
    values = dict(config.SETTING_DEFAULTS)
    with tx() as conn:
        for row in conn.execute("SELECT key, value FROM settings"):
            try:
                values[row["key"]] = json.loads(row["value"])
            except Exception:
                values[row["key"]] = row["value"]
    return values


def set_settings(patch: dict[str, Any]) -> dict[str, Any]:
    with tx() as conn:
        for key, value in patch.items():
            if key not in config.SETTING_DEFAULTS:
                continue
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
    return get_settings()


# --------------------------------------------------------------------------- #
# clips
# --------------------------------------------------------------------------- #
def create_clip(
    *,
    title: str,
    source_label: str,
    source_kind: str,
    fingerprint: Fingerprint,
    thumb: str,
    duration: float,
    width: int,
    height: int,
    cluster_id: int | None = None,
) -> dict:
    stamp = now()
    payload = fingerprint.to_json()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO clips (title, source_label, source_kind, duration, width, height,
                                  frames, fingerprint, thumb, status, cluster_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                title, source_label, source_kind, duration, width, height,
                len(fingerprint.frames), json.dumps(payload), thumb,
                "analyzed", cluster_id, stamp, stamp,
            ),
        )
        clip_id = cur.lastrowid
    return get_clip(clip_id)


def get_clip(clip_id: int, with_candidates: bool = True) -> dict | None:
    with tx() as conn:
        clip = row_to_dict(conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone())
        if not clip:
            return None
        clip["fingerprint_data"] = _safe_json(clip.get("fingerprint"), {})
        clip["fingerprint"] = clip["fingerprint_data"]  # keep API simple
        clip["tags"] = [t for t in (clip.get("tags") or "").split(",") if t]
        if with_candidates:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE clip_id=? ORDER BY confirmed DESC, score DESC, id ASC",
                (clip_id,),
            ).fetchall()
            clip["candidates"] = [_candidate_dict(r) for r in rows]
        runs = conn.execute(
            "SELECT provider, status, found, ms, error, created_at FROM search_runs "
            "WHERE clip_id=? ORDER BY id DESC LIMIT 16",
            (clip_id,),
        ).fetchall()
        clip["runs"] = [dict(r) for r in runs]
        return clip


def list_clips(query: str = "", status: str = "", cluster_id: int | None = None,
               limit: int = 300) -> list[dict]:
    sql = "SELECT * FROM clips WHERE 1=1"
    params: list[Any] = []
    if query:
        sql += " AND (title LIKE ? OR notes LIKE ? OR used_in LIKE ? OR source_label LIKE ? OR tags LIKE ?)"
        like = f"%{query}%"
        params += [like] * 5
    if status:
        sql += " AND status=?"
        params.append(status)
    if cluster_id is not None:
        sql += " AND cluster_id=?"
        params.append(cluster_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with tx() as conn:
        rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            # fingerprints are big and the list view never needs them
            item.pop("fingerprint", None)
            item["tags"] = [t for t in (item.get("tags") or "").split(",") if t]
            out.append(item)
        return out


def update_clip(clip_id: int, patch: dict[str, Any]) -> dict | None:
    allowed = {"title", "notes", "used_in", "tags", "status", "verdict", "match_score",
               "cluster_id", "best_source", "times_used", "last_searched", "thumb",
               "frames", "duration", "width", "height"}
    fields, params = [], []
    for key, value in patch.items():
        if key not in allowed:
            continue
        if key == "tags" and isinstance(value, list):
            value = ",".join(str(v).strip() for v in value if str(v).strip())
        fields.append(f"{key}=?")
        params.append(value)
    if not fields:
        return get_clip(clip_id)
    fields.append("updated_at=?")
    params.append(now())
    params.append(clip_id)
    with tx() as conn:
        conn.execute(f"UPDATE clips SET {', '.join(fields)} WHERE id=?", params)
    return get_clip(clip_id)


def delete_clip(clip_id: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM candidates WHERE clip_id=?", (clip_id,))
        conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))


def mark_used(clip_id: int, used_in: str = "", times: int | None = None) -> dict | None:
    clip = get_clip(clip_id, with_candidates=False)
    if not clip:
        return None
    new_times = times if times is not None else int(clip["times_used"]) + 1
    return update_clip(clip_id, {"times_used": new_times, "used_in": used_in or clip["used_in"]})


def add_candidate(clip_id: int, **kwargs: Any) -> dict:
    stamp = now()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO candidates (clip_id, provider, url, canonical, host, title, author,
                                       published, kind, thumb, score, reasons, mentions, manual,
                                       confirmed, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                clip_id, kwargs.get("provider", "manual"), kwargs.get("url", ""),
                kwargs.get("canonical", ""), kwargs.get("host", ""), kwargs.get("title", ""),
                kwargs.get("author", ""), kwargs.get("published", ""), kwargs.get("kind", "page"),
                kwargs.get("thumb", ""), float(kwargs.get("score", 0)),
                json.dumps(kwargs.get("reasons", [])), int(kwargs.get("mentions", 1)),
                1 if kwargs.get("manual") else 0, 1 if kwargs.get("confirmed") else 0, stamp,
            ),
        )
        cid = cur.lastrowid
        row = conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        return _candidate_dict(row)


def candidate_exists(clip_id: int, canonical: str) -> bool:
    with tx() as conn:
        row = conn.execute(
            "SELECT id FROM candidates WHERE clip_id=? AND canonical=?", (clip_id, canonical)
        ).fetchone()
        return row is not None


def update_candidate(candidate_id: int, patch: dict[str, Any]) -> None:
    allowed = {"score", "reasons", "mentions", "confirmed", "title", "author", "thumb", "published"}
    fields, params = [], []
    for key, value in patch.items():
        if key not in allowed:
            continue
        if key == "reasons":
            value = json.dumps(value)
        fields.append(f"{key}=?")
        params.append(value)
    if not fields:
        return
    params.append(candidate_id)
    with tx() as conn:
        conn.execute(f"UPDATE candidates SET {', '.join(fields)} WHERE id=?", params)


def confirmed_source(clip_id: int) -> dict | None:
    with tx() as conn:
        row = conn.execute(
            "SELECT * FROM candidates WHERE clip_id=? AND confirmed=1 ORDER BY id DESC LIMIT 1",
            (clip_id,),
        ).fetchone()
        return _candidate_dict(row) if row else None


def _candidate_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["reasons"] = _safe_json(item.get("reasons"), [])
    item["manual"] = bool(item.get("manual"))
    item["confirmed"] = bool(item.get("confirmed"))
    return item


def _safe_json(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# search runs
# --------------------------------------------------------------------------- #
def start_run(clip_id: int, provider: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO search_runs (clip_id, provider, status, created_at) VALUES (?,?,?,?)",
            (clip_id, provider, "running", now()),
        )
        return cur.lastrowid


def finish_run(run_id: int, status: str, found: int = 0, ms: int = 0, error: str = "") -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE search_runs SET status=?, found=?, ms=?, error=? WHERE id=?",
            (status, found, ms, error[:500], run_id),
        )


# --------------------------------------------------------------------------- #
# duplicate lookups
# --------------------------------------------------------------------------- #
def similar_clips(fingerprint: Fingerprint, exclude_id: int | None = None,
                  settings: dict | None = None, limit: int = 6) -> list[dict]:
    """Compare a fingerprint against everything already in the library."""
    settings = settings or get_settings()
    dup_mean = float(settings.get("dup_mean_distance", config.DUP_MEAN_DISTANCE))
    dup_cov = float(settings.get("dup_coverage", config.DUP_COVERAGE))

    with tx() as conn:
        rows = conn.execute(
            "SELECT id, title, thumb, status, times_used, used_in, notes, cluster_id, "
            "verdict, created_at, fingerprint FROM clips"
            + (" WHERE id<>?" if exclude_id else ""),
            (exclude_id,) if exclude_id else (),
        ).fetchall()

    results = []
    for row in rows:
        other = Fingerprint.from_json(_safe_json(row["fingerprint"], {}))
        cmp = compare(fingerprint, other, dup_mean, dup_cov)
        if cmp.verdict == "distinct" or cmp.score <= 0:
            continue
        results.append({
            "id": row["id"],
            "title": row["title"],
            "thumb": row["thumb"],
            "status": row["status"],
            "times_used": row["times_used"],
            "used_in": row["used_in"],
            "notes": row["notes"],
            "cluster_id": row["cluster_id"],
            "created_at": row["created_at"],
            "comparison": cmp.to_dict(),
        })
    results.sort(key=lambda r: r["comparison"]["score"], reverse=True)
    return results[:limit]


_cluster_cache: dict[str, Any] = {"key": None, "value": []}


def clusters(settings: dict | None = None) -> list[dict]:
    """Group the library by similarity so repeats are obvious."""
    settings = settings or get_settings()
    dup_mean = float(settings.get("dup_mean_distance", config.DUP_MEAN_DISTANCE))
    dup_cov = float(settings.get("dup_coverage", config.DUP_COVERAGE))

    with tx() as conn:
        stamp = conn.execute(
            "SELECT COUNT(*) c, COALESCE(MAX(updated_at), '') t, COALESCE(SUM(times_used), 0) u FROM clips"
        ).fetchone()
        cache_key = f"{stamp['c']}|{stamp['t']}|{stamp['u']}|{dup_mean}|{dup_cov}"
        if _cluster_cache["key"] == cache_key:
            return _cluster_cache["value"]
        rows = conn.execute(
            "SELECT id, title, thumb, duration, times_used, used_in, cluster_id, "
            "created_at, fingerprint FROM clips ORDER BY id ASC"
        ).fetchall()

    items = [(dict(r), Fingerprint.from_json(_safe_json(r["fingerprint"], {}))) for r in rows]
    if len(items) < 2:
        _cluster_cache.update(key=cache_key, value=[])
        return []

    # union-find over pairwise "duplicate" verdicts
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    best: dict[tuple[int, int], dict] = {}
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            cmp = compare(items[i][1], items[j][1], dup_mean, dup_cov)
            if cmp.verdict == "duplicate":
                best[(i, j)] = cmp.to_dict()
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    buckets: dict[int, list[dict]] = {}
    for idx, (row, _fp) in enumerate(items):
        buckets.setdefault(find(idx), []).append(row)
    groups = [g for g in buckets.values() if len(g) >= 2]
    result = [
        {
            "key": f"{min(m['id'] for m in g)}-{len(g)}",
            "size": len(g),
            "total_uses": sum(m["times_used"] for m in g),
            "members": [{**m, "used_in": m.get("used_in", "")} for m in g],
        }
        for g in groups
    ]
    _cluster_cache.update(key=cache_key, value=result)
    return result


def stats() -> dict:
    with tx() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM clips").fetchone()["c"]
        searched = conn.execute("SELECT COUNT(*) c FROM clips WHERE last_searched<>''").fetchone()["c"]
        sourced = conn.execute("SELECT COUNT(*) c FROM clips WHERE best_source<>''").fetchone()["c"]
        used = conn.execute("SELECT COUNT(*) c FROM clips WHERE times_used>0").fetchone()["c"]
        cands = conn.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"]
    return {
        "clips": total, "searched": searched, "sourced": sourced,
        "used": used, "candidates": cands,
    }
