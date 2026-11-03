"""Unattended coaching runs after each sync: the morning check-in once the night's sleep is in, and a review of
every newly analyzed session. Each run is a headless Claude Code session (`claude -p /auto-...`) in this checkout,
so it has the coach MCP server, CLAUDE.md and the skills. Nobody answers while it runs, so the auto-* skills decide
without waiting; questions they ask with ask_athlete show on the dashboard, and the athlete's answer resumes the same
session as a dashboard chat (chats.py)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta
from typing import Any

from . import chats, health, settings, store
from .config import DATA_DIR, NON_TRAINING_SPORTS, ROOT

_LOGGER = logging.getLogger(__name__)
REPORT_DIR = DATA_DIR / "auto"
# Morning check-in: not before CHECKIN_FROM; once the night's sleep and HRV are synced, or at CHECKIN_BY with
# whatever is there (the skill then says what is missing). After CHECKIN_UNTIL the day is skipped.
CHECKIN_FROM, CHECKIN_BY, CHECKIN_UNTIL = "04:30", "10:30", "13:00"
REVIEW_WINDOW_DAYS = 2      # only sessions from today and yesterday (never a backfill)
MAX_REVIEWS_PER_RUN = 3
MAX_TRIES = 2               # per check-in / session, so a failing run does not burn the Claude plan every 10 min
RUN_TIMEOUT_S = 20 * 60

# Hard limits for unattended runs, on top of what the skills say: read everything, write the check-in, review, coach
# note and suggestions, never the plan itself. Thresholds, weight, profile, race and nutrition plans need the
# athlete's yes in a chat (the athlete's answer to a question resumes the run as a chat, with chat permissions). Applying a suggestion the athlete accepted on the dashboard may also change that session
# and update it on the COROS calendar.
_ALLOWED = ["mcp__coach", "Skill", "Read", "Glob", "Grep"]
_NEVER = ["AskUserQuestion", "Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
          *(f"mcp__coach__{t}" for t in ("update_athlete_profile", "save_plan", "clear_plan", "log_weight",
                                         "save_race_plan", "save_nutrition_plan", "recompute_metrics",
                                         "coros_raw", "garmin_raw"))]
_PLAN_WRITES = ["mcp__coach__adjust_planned_session", "mcp__coach__push_session_to_coros"]


def enabled() -> set[str]:
    """COACH_AUTO in coach.env, e.g. "checkin,review". Off unless set: every run uses the athlete's Claude plan."""
    return {s.strip() for s in os.environ.get("COACH_AUTO", "").split(",") if s.strip()}


def _state() -> dict[str, Any]:
    raw = store.get_meta("auto_state")
    return json.loads(raw) if raw else {}


def _save_state(state: dict[str, Any]) -> None:
    store.set_meta("auto_state", json.dumps(state))


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def _manual_checkin_today(today: str) -> bool:
    if store.checkin(today):
        return True
    raw = store.get_meta("coach_note")
    note = json.loads(raw) if raw else {}
    return note.get("kind") == "checkin" and str(note.get("updated", "")).startswith(today)


def checkin_due(state: dict[str, Any], today: date) -> str | None:
    """Why the check-in should run now, or None."""
    day = today.isoformat()
    done = state.get("checkin") or {}
    if done.get("date") == day and (done.get("done") or done.get("tries", 0) >= MAX_TRIES):
        return None
    now = _now_hm()
    if not CHECKIN_FROM <= now < CHECKIN_UNTIL or _manual_checkin_today(day):
        return None
    night = health.rows(today, today)[0]
    if night.get("sleep_h") and night.get("hrv"):
        return "sleep and HRV synced"
    if now >= CHECKIN_BY:
        return "no full night data by " + CHECKIN_BY
    return None


def new_sessions(state: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """Training sessions whose FIT file has been analyzed and that were not reviewed yet (oldest first)."""
    since = (today - timedelta(days=REVIEW_WINDOW_DAYS - 1)).isoformat()
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, date, start_local, sport, name FROM activities WHERE date >= ? AND fit_status = 'ok' "
            "ORDER BY start_local, id", (since,))]
    rows = [r for r in rows if r["sport"] not in NON_TRAINING_SPORTS]
    if "reviewed" not in state:  # first run: start from now, do not review what is already there
        state["reviewed"] = [r["id"] for r in rows]
        return []
    tries, due = state.get("review_tries", {}), []
    for r in rows:
        if r["id"] in state["reviewed"] or tries.get(str(r["id"]), 0) >= MAX_TRIES:
            continue
        if store.session_review(r["id"]):  # already reviewed in a chat: keep that one
            state["reviewed"].append(r["id"])
            continue
        due.append(r)
    return due


def claude_path() -> str:
    path = os.environ.get("COACH_CLAUDE") or shutil.which("claude")
    if not path:
        raise RuntimeError("claude not found on PATH (set COACH_CLAUDE in coach.env)")
    return path


def run_claude(prompt: str, report_name: str, apply: bool = False,
               chat: dict[str, Any] | None = None) -> tuple[bool, str]:
    """One headless Claude Code session in this checkout. The reply is saved to data/auto/<report_name>.md.
    apply: the run applies a suggestion the athlete accepted, so it may change the plan.
    chat: {kind, title, activity_id?, day?} makes the run a dashboard conversation the athlete can answer."""
    denied = _NEVER + ([] if apply else _PLAN_WRITES)
    cmd = [claude_path(), "-p", prompt, "--output-format", "text",
           "--allowedTools", ",".join(_ALLOWED), "--disallowedTools", ",".join(denied)]
    env = None
    chat_id = chats.begin_auto(**chat) if chat else None
    if chat_id:
        cmd += ["--session-id", chat_id]
        env = {**os.environ, "COACH_CHAT_ID": chat_id}  # questions it asks belong to this session
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                              timeout=RUN_TIMEOUT_S, stdin=subprocess.DEVNULL, env=env)
        ok, out = proc.returncode == 0 and bool(proc.stdout.strip()), proc.stdout.strip() or proc.stderr.strip()
    except subprocess.TimeoutExpired:
        ok, out = False, f"timed out after {RUN_TIMEOUT_S // 60} min"
    if chat_id:
        chats.finish_auto(chat_id, ok, out)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="minutes")
    (REPORT_DIR / f"{report_name}.md").write_text(
        f"<!-- {prompt} · {stamp} · {round(time.time() - started)} s · {'ok' if ok else 'FAILED'} -->\n\n{out}\n",
        encoding="utf-8")
    return ok, out[:300]


def run(only: set[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Start what is due. `only` overrides COACH_AUTO (e.g. from the command line)."""
    kinds = only if only is not None else enabled()
    if not kinds:
        return {"skipped": "COACH_AUTO is not set (e.g. COACH_AUTO=checkin,review in coach.env)"}
    if settings.provider() is None:
        return {"skipped": "no data source connected"}
    today = date.today()
    state, done = _state(), []

    reason = checkin_due(state, today) if "checkin" in kinds else None
    if reason:
        entry = state.get("checkin") if (state.get("checkin") or {}).get("date") == today.isoformat() else None
        entry = entry or {"date": today.isoformat(), "tries": 0}
        if dry_run:
            done.append({"checkin": reason, "dry_run": True})
        else:
            entry["tries"] += 1
            state["checkin"] = entry
            _save_state(state)  # count the try before the run, so a crash cannot loop
            ok, out = run_claude("/auto-checkin", f"{today.isoformat()}-checkin",
                                 chat={"kind": "checkin", "title": f"Check-in {today.isoformat()}",
                                       "day": today.isoformat()})
            if ok and store.checkin(today.isoformat()) is None:
                report = (REPORT_DIR / f"{today.isoformat()}-checkin.md").read_text(encoding="utf-8")
                store.put_checkin(today.isoformat(), report.split("-->", 1)[-1], "auto")
            entry["done"] = ok
            done.append({"checkin": reason, "ok": ok, **({} if ok else {"error": out})})

    sessions = new_sessions(state, today) if "review" in kinds else []
    for a in sessions[:MAX_REVIEWS_PER_RUN]:
        if dry_run:
            done.append({"review": a, "dry_run": True})
            continue
        tries = state.setdefault("review_tries", {})
        tries[str(a["id"])] = tries.get(str(a["id"]), 0) + 1
        _save_state(state)
        ok, out = run_claude(f"/auto-session-review {a['id']}", f"{a['date']}-review-{a['id']}",
                             chat={"kind": "session-review", "activity_id": a["id"], "day": a["date"],
                                   "title": f"Review: {a['name'] or a['sport']} {a['date']}"})
        if ok and store.session_review(a["id"]) is None:
            report = (REPORT_DIR / f"{a['date']}-review-{a['id']}.md").read_text(encoding="utf-8")
            store.put_session_review(a["id"], report.split("-->", 1)[-1], "auto")
        if ok:
            state["reviewed"].append(a["id"])
            tries.pop(str(a["id"]), None)
        done.append({"review": a["id"], "sport": a["sport"], "ok": ok, **({} if ok else {"error": out})})

    if not dry_run:
        state["reviewed"] = state.get("reviewed", [])[-200:]
        _save_state(state)
    return {"ran": done} if done else {"nothing_due": True}
