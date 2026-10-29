"""SQLite cache: activity summaries + computed metrics, parsed per-day health records, and the plan."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH, FIT_DIR, FIT_SPORTS, LEGACY_DBS

_LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    start_local TEXT,
    sport TEXT NOT NULL,                       -- bike | run | swim | strength | multisport | mobility | walk | other
    sport_type INTEGER,                        -- COROS sportType code (Garmin: NULL, see type)
    type TEXT,
    name TEXT,
    summary_json TEXT NOT NULL,
    fit_status TEXT NOT NULL DEFAULT 'none',   -- none | pending | ok | missing | error
    metrics_json TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
CREATE TABLE IF NOT EXISTS daily (
    source TEXT NOT NULL,
    date TEXT NOT NULL,
    json TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (source, date)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS body_log (                -- weigh-ins logged by hand; kept when the source changes
    date TEXT PRIMARY KEY,
    weight_kg REAL NOT NULL,
    note TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phase TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    goal TEXT,
    notes TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS planned_sessions (
    date TEXT NOT NULL,
    slot INTEGER NOT NULL DEFAULT 1,
    block_id INTEGER,
    sport TEXT NOT NULL DEFAULT 'bike',
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    duration_min REAL,
    tss REAL,
    targets TEXT,
    structure TEXT,
    purpose TEXT,
    status TEXT NOT NULL DEFAULT 'planned',   -- planned | adjusted | skipped | added
    original_json TEXT,                       -- the session as first planned, kept when adjusted
    change_reason TEXT,
    coros_ref TEXT,                           -- idInPlan once pushed to the COROS calendar
    updated_at TEXT,
    PRIMARY KEY (date, slot)
);
"""


# Meta keys that belong to the athlete, not to the data source's cache; kept when the source changes.
_KEEP_META = ("coach_note", "nutrition_plan")
_prepared = False


def _adopt_legacy_db() -> str | None:
    """Take over the cache of the separate Garmin or COROS coach this app grew out of."""
    if DB_PATH.exists():
        return None
    for provider, path in LEGACY_DBS.items():
        if path.exists():
            path.rename(DB_PATH)
            return provider
    return None


def _migrate(conn: sqlite3.Connection, legacy: str | None) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)")}
    if "sport" not in cols:  # the Garmin coach's schema: rides only, raw Garmin summaries
        conn.execute("ALTER TABLE activities ADD COLUMN sport TEXT NOT NULL DEFAULT 'other'")
        conn.execute("ALTER TABLE activities ADD COLUMN sport_type INTEGER")
        from .garmin_sync import activity_row

        for r in conn.execute("SELECT id, fit_status, summary_json FROM activities").fetchall():
            row = activity_row(json.loads(r["summary_json"]))
            fit_status = r["fit_status"] if r["fit_status"] != "none" or row["sport"] not in FIT_SPORTS else "pending"
            conn.execute("UPDATE activities SET sport=?, type=?, summary_json=?, fit_status=? WHERE id=?",
                         (row["sport"], row["type"], json.dumps(row["summary"]), fit_status, r["id"]))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('recompute_needed', '1')")
        _LOGGER.warning("migrated the Garmin cache to the multi-sport schema")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(planned_sessions)")}
    if "sport" not in pcols:
        conn.execute("ALTER TABLE planned_sessions ADD COLUMN sport TEXT NOT NULL DEFAULT 'bike'")
        conn.execute("UPDATE planned_sessions SET sport='rest' WHERE type='rest'")
        conn.execute("UPDATE planned_sessions SET sport='strength' WHERE type='strength'")
    if "coros_ref" not in pcols:
        conn.execute("ALTER TABLE planned_sessions ADD COLUMN coros_ref TEXT")
    if legacy:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('cache_provider', ?)", (legacy,))
        from . import settings

        if settings.provider() is None:  # keep coaching as before: Garmin was the cycling coach, COROS the tri coach
            settings.save(provider=legacy, focus="road_cycling" if legacy == "garmin" else "triathlon")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    global _prepared
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    legacy = None if _prepared else _adopt_legacy_db()
    # Hourly sync, MCP auto-sync and manual syncs can overlap; wait for the write lock instead of failing.
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    if not _prepared:
        _migrate(conn, legacy)
        conn.commit()
        _prepared = True
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_meta(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def upsert_activity(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    """Insert or refresh an activity summary. Returns True if it was new."""
    existing = conn.execute("SELECT fit_status FROM activities WHERE id = ?", (row["id"],)).fetchone()
    if existing:
        conn.execute(
            "UPDATE activities SET date=?, start_local=?, sport=?, sport_type=?, type=?, name=?, summary_json=?, "
            "updated_at=? WHERE id=?",
            (row["date"], row["start_local"], row["sport"], row["sport_type"], row["type"], row["name"],
             json.dumps(row["summary"]), _now(), row["id"]),
        )
        return False
    conn.execute(
        "INSERT INTO activities(id, date, start_local, sport, sport_type, type, name, summary_json, fit_status, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["date"], row["start_local"], row["sport"], row["sport_type"], row["type"], row["name"],
         json.dumps(row["summary"]), "pending" if row["fit"] else "none", _now()),
    )
    return True


def set_activity_metrics(conn: sqlite3.Connection, activity_id: int, metrics: dict, fit_status: str | None = None) -> None:
    if fit_status:
        conn.execute(
            "UPDATE activities SET metrics_json=?, fit_status=?, updated_at=? WHERE id=?",
            (json.dumps(metrics), fit_status, _now(), activity_id),
        )
    else:
        conn.execute(
            "UPDATE activities SET metrics_json=?, updated_at=? WHERE id=?",
            (json.dumps(metrics), _now(), activity_id),
        )


def activities(since: str | None = None, until: str | None = None, sport: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM activities WHERE 1=1"
    args: list[Any] = []
    if since:
        q += " AND date >= ?"
        args.append(since)
    if until:
        q += " AND date <= ?"
        args.append(until)
    if sport:
        q += " AND sport = ?"
        args.append(sport)
    q += " ORDER BY start_local"
    with connect() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_activity_dict(r) for r in rows]


def activity(activity_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    return _activity_dict(row) if row else None


def latest_activity(sport: str | None = None) -> dict[str, Any] | None:
    """Latest training activity (of one sport if given)."""
    q = "SELECT * FROM activities WHERE sport NOT IN ('mobility', 'walk')" + (" AND sport = ?" if sport else "")
    with connect() as conn:
        row = conn.execute(q + " ORDER BY start_local DESC LIMIT 1", (sport,) if sport else ()).fetchone()
    return _activity_dict(row) if row else None


def _activity_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["summary"] = json.loads(d.pop("summary_json"))
    d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
    return d


def put_daily(conn: sqlite3.Connection, source: str, day: str, payload: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO daily(source, date, json, updated_at) VALUES (?, ?, ?, ?)",
        (source, day, json.dumps(payload), _now()),
    )


def daily(since: str, until: str) -> dict[str, dict[str, Any]]:
    """{date: {source: payload}}"""
    out: dict[str, dict[str, Any]] = {}
    with connect() as conn:
        for r in conn.execute(
            "SELECT source, date, json FROM daily WHERE date >= ? AND date <= ? ORDER BY date", (since, until)
        ):
            out.setdefault(r["date"], {})[r["source"]] = json.loads(r["json"])
    return out


def daily_date_span(source: str) -> tuple[str | None, str | None]:
    with connect() as conn:
        row = conn.execute("SELECT MIN(date) AS lo, MAX(date) AS hi FROM daily WHERE source = ?", (source,)).fetchone()
    return row["lo"], row["hi"]


def clear_cache(provider: str) -> None:
    """Drop everything synced from the previous data source. The plan, coach note and profile stay."""
    with connect() as conn:
        conn.execute("DELETE FROM activities")
        conn.execute("DELETE FROM daily")
        conn.execute(f"DELETE FROM meta WHERE key NOT IN ({','.join('?' * len(_KEEP_META))})", _KEEP_META)
        conn.execute("UPDATE planned_sessions SET coros_ref = NULL")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('cache_provider', ?)", (provider,))
    shutil.rmtree(FIT_DIR, ignore_errors=True)
