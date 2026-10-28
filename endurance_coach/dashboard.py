"""Data for the web dashboard: plan, health at a glance, load and the coach's latest note."""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Any

from . import athlete, coach, health, plan, settings, store
from .config import NON_TRAINING_SPORTS

_HISTORY_DAYS = 90
_MAX_HISTORY_WEEKS = 12
_MAX_PLAN_WEEKS = 52
_SESSION_DAYS = 42
_MAX_SESSIONS = 60


def set_note(text: str, kind: str = "checkin") -> dict[str, Any]:
    note = {"text": text.strip(), "kind": kind, "updated": datetime.now().isoformat(timespec="minutes")}
    store.set_meta("coach_note", json.dumps(note))
    return note


def _note() -> dict[str, Any] | None:
    raw = store.get_meta("coach_note")
    return json.loads(raw) if raw else None


def last_sync() -> str | None:
    raw = store.get_meta("last_sync")
    return datetime.fromtimestamp(float(raw)).isoformat(timespec="minutes") if raw else None


def _plan_range(today: date) -> tuple[date, date]:
    """Monday from which to show the plan (covers the current block, max 12 weeks back) and the Sunday it ends."""
    with store.connect() as conn:
        row = conn.execute("SELECT MAX(date) AS d FROM planned_sessions").fetchone()
    plan_end = date.fromisoformat(row["d"]) if row and row["d"] else today
    current = plan.blocks(today, today)
    first = min([today - timedelta(days=7)] + [date.fromisoformat(b["start_date"]) for b in current])
    first = max(first, today - timedelta(weeks=_MAX_HISTORY_WEEKS))
    start = first - timedelta(days=first.weekday())
    end = min(max(plan_end, today + timedelta(days=13)), start + timedelta(weeks=_MAX_PLAN_WEEKS) - timedelta(days=1))
    return start, end + timedelta(days=6 - end.weekday())


def _plan(today: date) -> dict[str, Any]:
    """The whole visible plan: blocks and weeks of days, with plan-vs-actual on past days."""
    start, end = _plan_range(today)
    planned: dict[str, list[dict]] = {}
    for s in plan.sessions(start, end):
        s["intensity"] = plan.INTENSITY_CLASS.get(s.get("type"), "easy")
        planned.setdefault(s["date"], []).append(s)
    comp = {d["date"]: d for d in plan.compliance(start, today)["days"]}

    weeks = []
    d = start
    while d <= end:
        days = []
        for i in range(7):
            day = d + timedelta(days=i)
            key = day.isoformat()
            c = comp.get(key, {})
            days.append({
                "date": key,
                "is_today": day == today,
                "is_past": day < today,
                "sessions": planned.get(key, []),
                "status": c.get("status"),
                "actual": c.get("actual") or [],
            })
        active = [s for x in days for s in x["sessions"] if s.get("status") != "skipped"]
        # Ridden without the planned intensity counts as partial, matching the badge on the page.
        statuses = ["partial" if "intensity missing" in (x["status"] or "") else (x["status"] or "").split(" ")[0]
                    for x in days]
        due = sum(st in ("done", "over", "partial", "missed") for st in statuses)
        score = sum(1.0 if st in ("done", "over") else 0.5 if st == "partial" else 0.0 for st in statuses)
        weeks.append({
            "week_of": d.isoformat(),
            "days": days,
            "planned_tss": round(sum(plan.estimated_tss(s) or 0 for s in active)),
            "planned_h": round(sum(s.get("duration_min") or 0 for s in active) / 60, 1),
            "actual_tss": round(sum((a.get("tss") or 0) for x in days for a in x["actual"])),
            "actual_h": round(sum((a.get("minutes") or 0) for x in days for a in x["actual"]) / 60, 1),
            "completion_pct": round(100 * score / due) if due else None,
            "block_ids": [b["id"] for b in plan.blocks(d, d + timedelta(days=6))],
        })
        d += timedelta(days=7)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "blocks": plan.blocks(start, end),
        "weeks": weeks,
        "has_sessions": bool(planned),
    }


def build() -> dict[str, Any]:
    today = date.today()
    profile = athlete.load()
    th = coach.thresholds(today)

    p = _plan(today)
    plan_end = date.fromisoformat(p["end"])
    # Only project forward when sessions are actually planned; otherwise a projection is just decay.
    has_future_plan = bool(plan.sessions(today + timedelta(days=1), plan_end))
    load = plan.projected_series(today - timedelta(days=_HISTORY_DAYS - 1), plan_end if has_future_plan else today)
    for row in load:
        row["projected"] = row["date"] > today.isoformat()

    hrows = health.rows(today - timedelta(days=_HISTORY_DAYS - 1), today)
    readiness = coach.readiness(today)
    events = []
    for e in profile.get("events") or []:
        if e.get("date", "") >= today.isoformat():
            proj = next((r for r in load if r["date"] == e["date"]), None)
            events.append({**e, "days_to_go": (date.fromisoformat(e["date"]) - today).days,
                           "projected_tsb": proj["tsb"] if proj else None})

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "last_sync": last_sync(),
        "app": settings.describe(),
        "sync_error": store.get_meta("sync_error"),
        "athlete": {"name": profile.get("name"), "goals": profile.get("goals") or [], **th,
                    "wkg": round(th["ftp"] / th["weight_kg"], 2) if th["ftp"] and th["weight_kg"] else None},
        "events": events,
        "note": _note(),
        "readiness": {k: readiness.get(k) for k in
                      ("status", "flags", "positives", "hrv", "resting_hr", "sleep", "recovery", "cycle",
                       "data_present")},
        "health": [{k: r[k] for k in ("date", "hrv", "hrv_baseline_low", "hrv_baseline_high", "rhr", "sleep_h",
                                       "sleep_score", "stress_avg", "recovery_pct", "bb_high", "bb_wake",
                                       "weight_kg")} for r in hrows],
        "weight_kg": th["weight_kg"],
        "load": load,
        "plan": p,
        "recent_sessions": [a for a in coach.list_activities(days=_SESSION_DAYS)
                            if a["sport"] not in NON_TRAINING_SPORTS][:_MAX_SESSIONS],
    }


def build_json() -> bytes:
    t = time.time()
    data = build()
    data["build_ms"] = round((time.time() - t) * 1000)
    return json.dumps(data, default=str).encode()
