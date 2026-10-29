"""Training plan: planned sessions and blocks, plan-vs-actual compliance and a forward CTL/ATL/TSB projection."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import athlete, metrics, store
from .config import NON_TRAINING_SPORTS
from .util import date_range, num, rnd, to_date

Sport = Literal["swim", "bike", "run", "brick", "strength", "mobility", "rest", "other"]
SessionType = Literal[
    "rest", "recovery", "endurance", "long", "technique", "tempo", "sweet_spot", "threshold", "vo2max",
    "anaerobic", "sprint", "brick", "race", "test", "strength", "other",
]
# Intensity class per session type, used for display (dashboard).
INTENSITY_CLASS = {
    "rest": "rest", "recovery": "easy", "endurance": "easy", "long": "easy", "technique": "easy",
    "strength": "easy", "other": "easy",
    "tempo": "moderate", "sweet_spot": "moderate", "sprint": "moderate", "brick": "moderate",
    "threshold": "hard", "vo2max": "hard", "anaerobic": "hard", "race": "hard", "test": "hard",
}
_INTENSITY_TYPES = {"sweet_spot", "threshold", "vo2max", "anaerobic", "race", "test"}
# Typical IF per session type, used to estimate TSS when a session has none.
_TYPICAL_IF = {
    "rest": 0.0, "recovery": 0.55, "endurance": 0.68, "long": 0.70, "technique": 0.60, "tempo": 0.80,
    "sweet_spot": 0.85, "threshold": 0.88, "vo2max": 0.85, "anaerobic": 0.80, "sprint": 0.70, "brick": 0.78,
    "race": 0.90, "test": 0.85, "strength": 0.55, "other": 0.65,
}
# Completed activity sports that satisfy a planned session's sport (None = anything counts).
_MATCHES: dict[str, set[str] | None] = {"brick": {"bike", "run", "multisport"}, "other": None,
                                        "strength": {"strength", "other"}, "mobility": None}


class PlannedSession(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    sport: Sport = Field("bike", description="swim | bike | run | brick (bike+run) | strength | mobility | rest | other")
    title: str = Field(description="Short name, e.g. 'Run 5x1 km @ threshold'")
    type: SessionType = "endurance"
    duration_min: float | None = None
    tss: float | None = Field(None, description="Planned TSS; estimated from type and duration if omitted")
    targets: str | None = Field(None, description="Key targets, e.g. '5x1 km @ 5:05-5:10/km (HR <175)', "
                                                    "'3x12 min HR 158-165 (Z4)' or '10x100 m @ 1:55/100 m (CSS)'")
    structure: str | None = Field(None, description="Full step-by-step workout incl. warm-up, cool-down, fueling")
    purpose: str | None = None
    slot: int = Field(1, description="Use 2 for a second session on the same day")


class PlanBlock(BaseModel):
    name: str = Field(description="e.g. 'Build 1 - Marmotte'")
    phase: str | None = Field(None, description="base | build | peak | taper | recovery | transition")
    start_date: str
    end_date: str
    goal: str | None = None
    notes: str | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def typical_if(session_type: str | None) -> float:
    return _TYPICAL_IF.get(session_type or "other", 0.65)


def estimated_tss(s: dict[str, Any]) -> float | None:
    if num(s.get("tss")) is not None:
        return float(s["tss"])
    if s.get("status") == "skipped" or s.get("type") == "rest" or s.get("sport") == "rest":
        return 0.0
    if not s.get("duration_min"):
        return None
    intensity = typical_if(s.get("type"))
    return round(s["duration_min"] / 60 * intensity**2 * 100)


def _session_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    original = d.pop("original_json", None)
    if original:
        d["originally"] = json.loads(original)
    d.pop("updated_at", None)
    if d.get("tss") is None:
        est = estimated_tss(d)
        if est is not None:
            d["tss_est"] = est
    return {k: v for k, v in d.items() if v is not None}


def sessions(start: date, end: date) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM planned_sessions WHERE date >= ? AND date <= ? ORDER BY date, slot",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [_session_dict(r) for r in rows]


def sessions_on(day: date | str) -> list[dict[str, Any]]:
    d = to_date(day)
    return sessions(d, d)


def blocks(start: date, end: date) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, phase, start_date, end_date, goal, notes FROM plan_blocks "
            "WHERE end_date >= ? AND start_date <= ? ORDER BY start_date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [{k: v for k, v in dict(r).items() if v is not None} for r in rows]


# ---------------------------------------------------------------- writes

def save(new_sessions: list[PlannedSession], block: PlanBlock | None = None,
         replace_existing: bool = True) -> dict[str, Any]:
    """Write sessions (and optionally a block). With replace_existing, planned sessions from today onward in the
    covered date range are removed first, so a re-planned week fully replaces the old one. Past days are kept."""
    if not new_sessions and not block:
        return {"error": "Nothing to save."}
    for s in new_sessions:
        to_date(s.date)  # validate
    today = date.today().isoformat()
    with store.connect() as conn:
        block_id = None
        if block:
            cur = conn.execute(
                "INSERT INTO plan_blocks(name, phase, start_date, end_date, goal, notes, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (block.name, block.phase, to_date(block.start_date).isoformat(), to_date(block.end_date).isoformat(),
                 block.goal, block.notes, _now()),
            )
            block_id = cur.lastrowid
        if new_sessions and replace_existing:
            lo = max(min(s.date for s in new_sessions), today)
            hi = max(s.date for s in new_sessions)
            if lo <= hi:
                conn.execute("DELETE FROM planned_sessions WHERE date >= ? AND date <= ?", (lo, hi))
        for s in new_sessions:
            conn.execute(
                "INSERT OR REPLACE INTO planned_sessions(date, slot, block_id, sport, type, title, duration_min, tss, "
                "targets, structure, purpose, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?)",
                (to_date(s.date).isoformat(), s.slot, block_id, s.sport, s.type, s.title, s.duration_min, s.tss,
                 s.targets, s.structure, s.purpose, _now()),
            )
    start = to_date(min(s.date for s in new_sessions)) if new_sessions else to_date(block.start_date)
    end = to_date(max(s.date for s in new_sessions)) if new_sessions else to_date(block.end_date)
    return {"saved_sessions": len(new_sessions), "block_id": block_id, "plan": overview(start, (end - start).days + 1)}


def adjust(day: str, slot: int = 1, reason: str | None = None, skip: bool = False,
           **changes: Any) -> dict[str, Any]:
    """Change one planned session (or add one where none exists), keeping the original for plan-vs-actual."""
    key = to_date(day).isoformat()
    changes = {k: v for k, v in changes.items() if v is not None}
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM planned_sessions WHERE date = ? AND slot = ?", (key, slot)).fetchone()
        if row is None:
            if skip or "title" not in changes:
                return {"error": f"No planned session on {key} slot {slot}; give at least a title to add one."}
            conn.execute(
                "INSERT INTO planned_sessions(date, slot, sport, type, title, duration_min, tss, targets, structure, "
                "purpose, status, change_reason, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'added', ?, ?)",
                (key, slot, changes.get("sport", "bike"), changes.get("type", "endurance"), changes["title"],
                 changes.get("duration_min"),
                 changes.get("tss"), changes.get("targets"), changes.get("structure"), changes.get("purpose"),
                 reason, _now()),
            )
        else:
            current = dict(row)
            original = current["original_json"] or json.dumps({
                k: current[k] for k in ("sport", "type", "title", "duration_min", "tss", "targets")
                if current[k] is not None})
            if skip:
                changes |= {"status": "skipped"}
            elif current["status"] != "added":
                changes |= {"status": "adjusted"}
            if "tss" not in changes and ({"duration_min", "type", "sport"} & changes.keys()):
                changes["tss"] = None  # stale; re-estimated from the new type/duration
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(
                f"UPDATE planned_sessions SET {sets}, original_json = ?, change_reason = ?, updated_at = ? "
                "WHERE date = ? AND slot = ?",
                (*changes.values(), original, reason, _now(), key, slot),
            )
    return {"session": sessions_on(key)}


def set_coros_ref(day: str, slot: int, ref: str) -> None:
    with store.connect() as conn:
        conn.execute("UPDATE planned_sessions SET coros_ref = ?, updated_at = ? WHERE date = ? AND slot = ?",
                     (ref, _now(), to_date(day).isoformat(), slot))


def session(day: str, slot: int = 1) -> dict[str, Any] | None:
    return next((s for s in sessions_on(day) if s.get("slot") == slot), None)


def clear(start_date: str, end_date: str, include_blocks: bool = False) -> dict[str, Any]:
    lo, hi = to_date(start_date).isoformat(), to_date(end_date).isoformat()
    with store.connect() as conn:
        n = conn.execute("DELETE FROM planned_sessions WHERE date >= ? AND date <= ?", (lo, hi)).rowcount
        b = conn.execute("DELETE FROM plan_blocks WHERE start_date >= ? AND end_date <= ?", (lo, hi)).rowcount \
            if include_blocks else 0
    return {"deleted_sessions": n, "deleted_blocks": b}


# ---------------------------------------------------------------- plan vs actual

def _training_activities(day: str) -> list[dict[str, Any]]:
    return [a for a in store.activities(since=day, until=day) if a["sport"] not in NON_TRAINING_SPORTS]


def _actual_view(a: dict[str, Any]) -> dict[str, Any]:
    s, m = a["summary"], a["metrics"]
    minutes = (num(s.get("duration_s")) or num(m.get("recorded_s")) or 0) / 60
    return {k: v for k, v in {
        "id": a["id"], "sport": a["sport"], "name": a["name"], "type": a["type"], "minutes": rnd(minutes),
        "km": rnd((num(s.get("distance_m")) or 0) / 1000, 2) or None,
        "tss": m.get("tss"), "tss_source": m.get("tss_source"), "if": m.get("if"), "avg_hr": m.get("avg_hr"),
    }.items() if v is not None}


def _sports_missing(planned: list[dict[str, Any]], actual: list[dict[str, Any]]) -> list[str]:
    """Planned sports with no matching activity that day (e.g. the swim of a swim + run day)."""
    done = {a["sport"] for a in actual}
    missing = []
    for s in planned:
        sport = s.get("sport") or "bike"
        allowed = _MATCHES.get(sport, {sport})
        if s.get("status") != "skipped" and allowed is not None and not (allowed & done):
            missing.append(sport)
    return missing


def compliance(start: date, end: date) -> dict[str, Any]:
    today = date.today()
    planned_by_day: dict[str, list[dict]] = {}
    for s in sessions(start, end):
        planned_by_day.setdefault(s["date"], []).append(s)

    days, totals = [], {"planned_tss": 0.0, "actual_tss": 0.0, "planned_h": 0.0, "actual_h": 0.0}
    counts = {"done": 0, "partial": 0, "over": 0, "missed": 0, "skipped": 0, "unplanned": 0, "intensity_missed": 0}
    for d in date_range(start, min(end, today)):
        key = d.isoformat()
        planned = [s for s in planned_by_day.get(key, []) if s.get("type") != "rest"]
        acts = _training_activities(key)
        actual = [_actual_view(a) for a in acts]
        p_tss = sum(estimated_tss(s) or 0 for s in planned if s.get("status") != "skipped")
        p_min = sum(s.get("duration_min") or 0 for s in planned if s.get("status") != "skipped")
        a_tss = sum(num(x.get("tss")) or 0 for x in actual)
        a_min = sum(x.get("minutes") or 0 for x in actual)
        totals["planned_tss"] += p_tss
        totals["actual_tss"] += a_tss
        totals["planned_h"] += p_min / 60
        totals["actual_h"] += a_min / 60

        if not planned and not actual:
            continue
        if planned and all(s.get("status") == "skipped" for s in planned):
            status = "skipped" if not actual else "unplanned"
        elif not planned:
            status = "unplanned"
        elif not actual:
            status = "pending" if d == today else "missed"
        else:
            ratio = a_tss / p_tss if p_tss and a_tss else (a_min / p_min if p_min else 1.0)
            status = "done" if 0.8 <= ratio <= 1.2 else "partial" if ratio < 0.8 else "over"
            missing = _sports_missing(planned, actual)
            if missing:
                status = f"partial ({'/'.join(missing)} missing)"
            planned_types = {s.get("type") for s in planned}
            best_if = max((num(x.get("if")) or 0 for x in actual), default=0)
            if planned_types & _INTENSITY_TYPES and best_if and best_if < 0.75 and not missing:
                counts["intensity_missed"] += 1
                status += " (intensity missing: IF {:.2f})".format(best_if)
        if status != "pending":
            counts[status.split(" ")[0]] += 1
        days.append({"date": key, "status": status,
                     "planned": [{k: s[k] for k in ("sport", "title", "type", "duration_min", "tss", "tss_est", "targets",
                                                   "status") if k in s} for s in planned],
                     "actual": actual})

    due = counts["done"] + counts["partial"] + counts["over"] + counts["missed"]
    summary = {k: rnd(v, 1) for k, v in totals.items()} | {k: v for k, v in counts.items() if v}
    if due:
        summary["completion_pct"] = rnd(100 * (counts["done"] + counts["over"] + 0.5 * counts["partial"]) / due)
    return {"summary": summary, "days": days}


# ---------------------------------------------------------------- projection

def projected_series(start: date, until: date) -> list[dict[str, Any]]:
    """Daily CTL/ATL/TSB from start to until: actual TSS up to today, planned TSS after
    (today: actual if already ridden, else the plan)."""
    from . import coach  # local import: coach imports this module

    today = date.today()
    daily = dict(coach._daily_tss(min(start, today) - timedelta(days=200)))
    planned_tss: dict[str, float] = {}
    for s in sessions(today, until):
        planned_tss[s["date"]] = planned_tss.get(s["date"], 0.0) + (estimated_tss(s) or 0.0)
    for key, value in planned_tss.items():
        if key == today.isoformat() and daily.get(key):
            continue
        daily[key] = value
    return metrics.pmc(daily, start, until, seed_days=200)


def projection(until: date) -> dict[str, Any]:
    today = date.today()
    series = projected_series(today, until)

    marks = {until.isoformat()}
    marks |= {r["date"] for r in series if to_date(r["date"]).weekday() == 6}
    events = [e for e in athlete.load().get("events") or [] if today.isoformat() <= e.get("date", "") <= until.isoformat()]
    marks |= {e["date"] for e in events}
    points = [r for r in series if r["date"] in marks]
    out: dict[str, Any] = {"weekly_and_end": points}
    if events:
        by_date = {r["date"]: r for r in series}
        out["events"] = [{**e, "projected": by_date.get(e["date"])} for e in events]
    out["note"] = "TSB = form going into the day. Days without planned sessions count as rest (TSS 0)."
    return out


def overview(start: date, days: int) -> dict[str, Any]:
    end = start + timedelta(days=days - 1)
    today = date.today()
    out: dict[str, Any] = {
        "range": f"{start}..{end}",
        "blocks": blocks(start, end),
        "sessions": sessions(start, end),
    }
    if start <= today:
        out["compliance"] = compliance(start, min(end, today))
    if end >= today:
        out["projection"] = projection(end)
    if not out["sessions"]:
        nxt = None
        with store.connect() as conn:
            row = conn.execute("SELECT MIN(date) AS d FROM planned_sessions WHERE date > ?", (end.isoformat(),)).fetchone()
            nxt = row["d"] if row else None
        out["note"] = f"No sessions planned in this range. Next planned session: {nxt or 'none'}."
    return out
