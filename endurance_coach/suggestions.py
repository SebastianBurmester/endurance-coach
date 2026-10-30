"""Plan changes a check-in or session review suggests. The athlete accepts or dismisses each one on the dashboard;
an accepted one is applied by a headless Claude Code run (`/apply-suggestion <id>`), one at a time."""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from typing import Any

from . import store
from .util import to_date

_LOGGER = logging.getLogger(__name__)
SOURCES = ("checkin", "session-review")
_SHOWN = ("open", "accepted", "applying")
_RECENT_DAYS = 2  # applied / failed / dismissed ones stay visible this long


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["changes"] = json.loads(d.pop("change_json") or "{}")
    return d


def add(day: str, title: str, reason: str, source: str, slot: int = 1, changes: dict[str, Any] | None = None,
        origin: str = "chat", activity_id: int | None = None) -> dict[str, Any]:
    """Save a suggestion. An open one for the same day and slot is superseded by it."""
    key = to_date(day).isoformat()
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    now = store._now()
    with store.connect() as conn:
        conn.execute("UPDATE suggestions SET status = 'superseded', result = 'replaced by a newer suggestion', "
                     "updated_at = ? WHERE date = ? AND slot = ? AND status = 'open'", (now, key, slot))
        cur = conn.execute(
            "INSERT INTO suggestions(date, slot, title, reason, change_json, source, origin, activity_id, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (key, slot, title.strip(), reason.strip(), json.dumps(changes or {}), source,
             "auto" if origin == "auto" else "chat", activity_id, now, now))
    return {"suggestion_id": cur.lastrowid, "status": "open",
            "note": "Shown on the dashboard with Accept / Dismiss; nothing in the plan changed yet."}


def get(suggestion_id: int) -> dict[str, Any] | None:
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return _row(row) if row else None


def listing(status: str | None = "open", today: date | None = None) -> list[dict[str, Any]]:
    """Suggestions by status (None: what the dashboard shows). Open ones for past days are left out."""
    today = today or date.today()
    with store.connect() as conn:
        if status:
            rows = conn.execute("SELECT * FROM suggestions WHERE status = ? ORDER BY date, slot, id", (status,))
        else:
            since = (today - timedelta(days=_RECENT_DAYS)).isoformat()
            rows = conn.execute(
                f"SELECT * FROM suggestions WHERE status IN ({','.join('?' * len(_SHOWN))}) OR updated_at >= ? "
                "ORDER BY date, slot, id", (*_SHOWN, since))
        out = [_row(r) for r in rows]
    return [s for s in out if s["status"] != "open" or s["date"] >= today.isoformat()]


def resolve(suggestion_id: int, status: str, result: str | None = None) -> dict[str, Any]:
    """Close a suggestion (Claude: after applying it, or when it no longer fits)."""
    if status not in ("applied", "dismissed", "superseded", "failed"):
        raise ValueError("status: applied | dismissed | superseded | failed")
    _set(suggestion_id, status, result)
    return {"suggestion_id": suggestion_id, "status": status}


def _set(suggestion_id: int, status: str, result: str | None = None) -> None:
    with store.connect() as conn:
        conn.execute("UPDATE suggestions SET status = ?, result = COALESCE(?, result), updated_at = ? WHERE id = ?",
                     (status, result, store._now(), suggestion_id))


# ---------------------------------------------------------------- the dashboard's buttons

_worker_lock = threading.Lock()
_worker: dict[str, threading.Thread | None] = {"thread": None}


def act(suggestion_id: int, action: str) -> dict[str, Any]:
    s = get(suggestion_id)
    if s is None:
        raise ValueError(f"no suggestion {suggestion_id}")
    if action == "dismiss" and s["status"] in ("open", "failed"):
        resolve(suggestion_id, "dismissed", "dismissed on the dashboard")
    elif action in ("accept", "retry") and s["status"] in ("open", "failed"):
        _set(suggestion_id, "accepted")
        start_worker()
    else:
        raise ValueError(f"cannot {action} a suggestion that is {s['status']}")
    return {"suggestions": listing(None)}


def start_worker() -> None:
    """Apply accepted suggestions one after another in a background thread (the dashboard polls the status)."""
    with _worker_lock:
        if _worker["thread"] and _worker["thread"].is_alive():
            return
        _worker["thread"] = threading.Thread(target=_drain, daemon=True, name="apply-suggestions")
        _worker["thread"].start()


def resume() -> None:
    """On dashboard start: a run cut off by a restart is tried again."""
    with store.connect() as conn:
        conn.execute("UPDATE suggestions SET status = 'accepted' WHERE status = 'applying'")
    if listing("accepted"):
        start_worker()


def _drain() -> None:
    from . import auto

    while queue := listing("accepted"):
        s = queue[0]
        _set(s["id"], "applying")
        try:
            ok, out = auto.run_claude(f"/apply-suggestion {s['id']}", f"{s['date']}-apply-{s['id']}", apply=True)
        except Exception as exc:  # noqa: BLE001
            ok, out = False, str(exc)
            _LOGGER.exception("applying suggestion %s failed", s["id"])
        after = get(s["id"]) or {}
        if after.get("status") == "applying":  # the run did not resolve it itself
            _set(s["id"], "applied" if ok else "failed", "applied" if ok else out[:300])
