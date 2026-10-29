"""MCP server exposing the coach tools to Claude. Source-specific tools are registered for the connected source only."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import athlete, coach, dashboard, plan, settings, sleep, sync, views
from .util import to_date

logging.basicConfig(level=logging.WARNING)

_APP = settings.describe()
_PROVIDER = _APP["provider"]
_WATCH = _APP["provider_label"]
settings.write_focus_pointer(_APP["focus"])


def _instructions() -> str:
    focus = "road-cycling" if _APP["focus"] == "road_cycling" else "triathlon"
    if _PROVIDER is None:
        source = ("No data source is connected yet: ask the athlete to open the dashboard's Settings tab and add a "
                  "Garmin Personal API key or connect COROS. Plan and profile tools still work.")
    else:
        source = f"Data comes from the athlete's {_WATCH} account, cached locally and auto-synced when older than 30 minutes."
    push = (" Planned run and bike sessions can be pushed to the COROS calendar with push_session_to_coros once the "
            "athlete wants that." if _PROVIDER == "coros" else
            " Workouts cannot be pushed to the watch; write them as a table the athlete can enter." if _PROVIDER else "")
    return (f"{focus.capitalize()} coach ({_APP['coaching_guide']} is the coaching guide). {source} Start a coaching "
            "conversation with daily_readiness and training_load; use analyze_session for a specific session. "
            "The training plan persists across sessions: read it with get_plan, write it with save_plan / "
            "adjust_planned_session. Thresholds (FTP, run threshold pace, CSS, LTHR) come from athlete.json first; "
            "ask the athlete before setting them." + push)


mcp = MCPServer(name="coach", instructions=_instructions())


def _prefetch() -> str | None:
    return sync.auto_sync()


def _wrap(payload: dict[str, Any], warning: str | None) -> dict[str, Any]:
    if warning:
        payload["sync_warning"] = warning
    return payload


@mcp.tool()
def sync_data(days_back: int | None = None, max_fit_downloads: int = 20) -> dict[str, Any]:
    """Pull new activities, FIT files and health data from the connected source (Garmin or COROS) into the cache.

    Incremental by default. days_back forces a re-fetch window (e.g. 365 for a first backfill).
    COROS allows ~50 FIT files per day; the rest stay pending for the next day (fit_pending_remaining).
    """
    try:
        return sync.sync_all(days_back, max_fit_downloads)
    except (sync.NotConnected, sync.SyncBusy) as exc:
        return {"error": str(exc)}


@mcp.tool()
def daily_readiness(day: str = "today") -> dict[str, Any]:
    """Morning readiness: last night's HRV (vs the watch's normal range and own baselines), resting HR, sleep,
    the watch's recovery gauge (COROS recovery % or Garmin Body Battery) and stress, plus training load
    (CTL/ATL/TSB) and yesterday's sessions. Returns status green/amber/red with the flags behind it. With COROS,
    `cycle` gives the menstrual cycle phase, cycle day and next period if the athlete tracks it (context, not a flag).
    day is YYYY-MM-DD or 'today'."""
    warning = _prefetch()
    return _wrap(coach.readiness(to_date(day)), warning)


@mcp.tool()
def training_load(days: int = 84) -> dict[str, Any]:
    """Performance Management Chart across all sports: current CTL (fitness), ATL (fatigue), TSB (form), ramp
    rate and ACWR, weekly volume per sport (sessions, hours, km, TSS, elevation, longest) and the last 14 days."""
    warning = _prefetch()
    return _wrap(coach.training_load(days), warning)


@mcp.tool()
def list_activities(days: int = 14, sport: str | None = None) -> dict[str, Any]:
    """Recent activities, newest first: sport, distance, time, pace/speed, TSS (+source), IF, power, HR, EF,
    decoupling, the watch's load/TE/RPE and the athlete's note. sport: swim | bike | run | strength | multisport."""
    warning = _prefetch()
    return _wrap({"activities": coach.list_activities(days, sport)}, warning)


@mcp.tool()
def analyze_session(activity_id: int | None = None, sport: str | None = None) -> dict[str, Any]:
    """Deep analysis of one session (default: the latest, optionally of one sport, e.g. 'bike' for the latest
    ride): TSS/IF, NP/VI/EF or pace/NGP, aerobic decoupling, time in HR and power zones, laps, best efforts, the
    watch's TE/load/RPE/note, the planned session for that day and form going in."""
    warning = _prefetch()
    return _wrap(coach.analyze_session(activity_id, sport), warning)


@mcp.tool()
def power_curve(days: int = 90, compare_days: int = 365) -> dict[str, Any]:
    """Bike mean-maximal power curve (1s-2h) in W and W/kg vs the longer window, with FTP / CP estimates.
    Only rides with power count."""
    warning = _prefetch()
    return _wrap(coach.power_curve(days, compare_days), warning)


@mcp.tool()
def zone_distribution(days: int = 28, sport: str | None = None) -> dict[str, Any]:
    """Time in HR zones per sport and bike power zones, the combined 3-zone split and the polarization index."""
    warning = _prefetch()
    return _wrap(coach.zone_distribution(days, sport), warning)


@mcp.tool()
def health_trends(days: int = 28) -> dict[str, Any]:
    """Daily and weekly-average HRV (with the watch's normal range), resting HR, sleep, sleep score, stress, the
    watch's recovery gauge (COROS recovery % or Body Battery) and weight. COROS adds its fitness assessment and,
    if tracked, the menstrual cycle (daily cycle_phase, recent and predicted cycles)."""
    warning = _prefetch()
    return _wrap(coach.health_trends(days), warning)


@mcp.tool()
def sleep_analysis(days: int = 180, include_nights: bool = False) -> dict[str, Any]:
    """Sleep in depth (nights dated by wake-up day): last night, 7/30-day and window averages vs the athlete's sleep
    need, sleep debt, a target bedtime, rhythm and regularity (bed / wake / midpoint spread, social jet lag), weekday
    pattern, score bands, and what the day before does to the night: training vs rest days, the hardest days, sessions
    ending < 3 h before bed (also hard ones), evening yoga / mobility, high-stress days, late bedtimes, weekends and
    (COROS) cycle phase. Each comparison gives per-metric differences with a verdict (clear / possible / none /
    too_few); correlations are Spearman. `findings` are the ready-made improve / good / info points.
    include_nights adds every night's record (bed, wake, stages, HR, HRV, the day's TSS and evening sessions)."""
    warning = _prefetch()
    return _wrap(sleep.analysis(days, include_nights=include_nights), warning)


@mcp.tool()
def progress_trends() -> dict[str, Any]:
    """Is the athlete getting fitter (last 365 days): threshold histories (FTP, run threshold, CSS) and what is in
    effect, CTL now vs 6 weeks ago and the year's peak, monthly hours per sport, monthly best power (5 s / 1 / 5 /
    20 min), power curve with FTP / CP estimates, and trends with a 6-vs-6-week verdict: endurance-ride efficiency
    (NP/HR), easy-run efficiency, HR drift on long runs, run cadence, VO2max, pool pace. `highlights` are ready-made."""
    warning = _prefetch()
    return _wrap(views.progress(), warning)


@mcp.tool()
def recovery_history(days: int = 120) -> dict[str, Any]:
    """Recovery over time: readiness status per day (green / amber / red with flags), HRV vs its 60-day range,
    resting HR delta, sleep and TSS per day; strain episodes (2+ days of red or HRV / RHR flags) with the load
    before them; the bounce-back curve after the biggest days (HRV %, RHR, sleep score vs the 30 days before,
    mornings 1-4); weekly averages and status counts."""
    warning = _prefetch()
    return _wrap(views.recovery(days), warning)


@mcp.tool()
def sport_overview(sport: str = "run") -> dict[str, Any]:
    """One sport (swim, bike, run, strength): volume for 7 / 28 / 84 / 365 days, 26 weeks of volume with run ramp
    (+10%) and long-run share (30%) checks, HR / power zones for 4 and 12 weeks, sport-specific trends (bike speed,
    efficiency, cadence and power curve; run pace, efficiency, cadence, long run; swim pace and distance),
    indoor / outdoor or pool / open-water split, and the last 12 weeks of sessions. `flags` are ready-made."""
    warning = _prefetch()
    return _wrap(views.sport_view(sport), warning)


@mcp.tool()
def race_overview() -> dict[str, Any]:
    """Every event in the profile: days to go, projected CTL / ATL / TSB on race day from the saved plan with a
    verdict against the +10 to +25 target, sessions and key sessions left, taper (block and TSS drop in the last 10
    days), the saved race plan and result; past events with what was recorded that day."""
    warning = _prefetch()
    return _wrap(views.races(), warning)


@mcp.tool()
def save_race_plan(event_date: str, race_plan: str | None = None, result: str | None = None,
                   event_name: str | None = None) -> dict[str, Any]:
    """Save the race plan for an event (shown on the dashboard's Races tab): pacing per discipline, fueling and
    fluids per hour, transitions, warm-up, logistics, written like a workout (markdown table, lists, 'Label: text').
    After the race, save `result` (times, splits, how it went). event_name picks between events on the same date."""
    return views.save_race_plan(event_date, race_plan, result, event_name)


@mcp.tool()
def week_summary(weeks_ago: int = 0) -> dict[str, Any]:
    """One training week (Mon-Sun): every session, totals per sport, HR / power zones, health averages, load at
    week end and plan vs actual. weeks_ago=0 is the current week, 1 the previous one."""
    warning = _prefetch()
    return _wrap(coach.week_summary(weeks_ago), warning)


@mcp.tool()
def get_athlete_profile() -> dict[str, Any]:
    """The athlete's profile (threshold histories per sport, LTHR, max HR, weight, goals, events, availability),
    the thresholds currently in effect (with where each comes from), days until each upcoming event, and the app
    settings: data source and coaching focus (road_cycling or triathlon) with the coaching guide to follow."""
    profile = athlete.load()
    today = date.today()
    for event in profile.get("events") or []:
        try:
            event["days_to_go"] = (to_date(event["date"]) - today).days
        except (KeyError, ValueError):
            pass
    out = {"app": settings.describe(), "profile": profile, "thresholds_in_effect": coach.thresholds(today)}
    if _PROVIDER == "coros":
        out |= {"coros_fitness_assessment": coach.meta_json("coros_fitness"), "coros_profile": coach.meta_json("coros_user")}
    return out


@mcp.tool()
def update_athlete_profile(
    ftp: float | None = None,
    run_threshold_pace: str | None = None,
    css_pace: str | None = None,
    threshold_date: str | None = None,
    threshold_source: str | None = None,
    weight_kg: float | None = None,
    lthr_bike: float | None = None,
    lthr_run: float | None = None,
    max_hr: float | None = None,
    weekly_hours_available: float | None = None,
    sleep_need_h: float | None = None,
    name: str | None = None,
    add_goal: str | None = None,
    add_event_name: str | None = None,
    add_event_date: str | None = None,
    add_event_priority: str | None = None,
    add_event_format: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Update athlete.json. Thresholds are appended to their history (dated threshold_date, default today):
    ftp in W, run_threshold_pace as 'm:ss' per km, css_pace as 'm:ss' per 100 m. TSS is recomputed afterwards.
    sleep_need_h (hours asleep the athlete feels best on; default 8) sets the target in sleep_analysis.
    Event priority is A/B/C; format e.g. gran fondo / road race / sprint / olympic / 70.3 / ironman. Only confirm
    values with the athlete."""
    profile = athlete.load()
    changed_thresholds = False
    for key, value in (("ftp_history", ftp), ("run_threshold_history", athlete.pace_to_s(run_threshold_pace)),
                       ("css_history", athlete.pace_to_s(css_pace))):
        if value:
            athlete.add_threshold(profile, key, float(value), threshold_date, threshold_source)
            changed_thresholds = True
    for key, value in (("weight_kg", weight_kg), ("lthr_bike", lthr_bike), ("lthr_run", lthr_run), ("max_hr", max_hr),
                       ("weekly_hours_available", weekly_hours_available), ("sleep_need_h", sleep_need_h), ("name", name), ("notes", notes)):
        if value is not None:
            profile[key] = value
    if add_goal:
        profile["goals"].append(add_goal)
    if add_event_name and add_event_date:
        event = {"date": add_event_date, "name": add_event_name, "priority": (add_event_priority or "B").upper()}
        if add_event_format:
            event["format"] = add_event_format
        profile["events"].append(event)
        profile["events"].sort(key=lambda e: e["date"])
    athlete.save(profile)
    result: dict[str, Any] = {"profile": profile}
    if changed_thresholds or lthr_bike or lthr_run or max_hr:
        # HR zones need the FIT streams re-read; threshold-only changes are cheap.
        result["recomputed"] = coach.recompute(full=bool(lthr_bike or lthr_run or max_hr))
    return result


@mcp.tool()
def get_plan(start_date: str = "today", days: int = 14) -> dict[str, Any]:
    """The training plan for a date range: blocks, planned sessions per sport (with any adjustments and the
    original), plan-vs-actual compliance for past days, and projected CTL/ATL/TSB through the range end
    (including projected form on event days). Use start_date in the past to review, e.g. last Monday."""
    warning = _prefetch()
    return _wrap(plan.overview(to_date(start_date), days), warning)


@mcp.tool()
def save_plan(sessions: list[plan.PlannedSession], block: plan.PlanBlock | None = None,
              replace_existing: bool = True) -> dict[str, Any]:
    """Save planned sessions, and optionally the training block they belong to. Include rest days as sport/type
    'rest'; use slot 2 for a second session on the same day (e.g. swim AM, run PM). With replace_existing
    (default), sessions from today onward inside the saved date range are replaced, so re-planning a week
    overwrites it cleanly; past days are never deleted. Give watts / paces / HR in targets and the full
    step-by-step workout in structure. Returns the saved plan with load projection."""
    return plan.save(sessions, block, replace_existing)


@mcp.tool()
def adjust_planned_session(
    date: str,
    reason: str,
    slot: int = 1,
    skip: bool = False,
    sport: plan.Sport | None = None,
    title: str | None = None,
    type: plan.SessionType | None = None,
    duration_min: float | None = None,
    tss: float | None = None,
    targets: str | None = None,
    structure: str | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Change one planned session (e.g. after a readiness check), skip it, or add a session on a day without
    one. The original version is kept so plan-vs-actual shows what changed and why."""
    result = plan.adjust(date, slot, reason, skip, sport=sport, title=title, type=type, duration_min=duration_min,
                         tss=tss, targets=targets, structure=structure, purpose=purpose)
    pushed = next((s for s in result.get("session") or [] if s.get("slot") == slot and s.get("coros_ref")), None)
    if pushed:
        result["coros_note"] = ("This session is on the COROS calendar: push_session_to_coros again to update it"
                                + (", or delete it in the COROS app (the API cannot remove workouts)." if skip else "."))
    return result


@mcp.tool()
def clear_plan(start_date: str, end_date: str, include_blocks: bool = False) -> dict[str, Any]:
    """Delete planned sessions in a date range (and blocks fully inside it if include_blocks). Confirm with
    the athlete first. Workouts already pushed to COROS stay there (delete them in the COROS app)."""
    return plan.clear(start_date, end_date, include_blocks)


@mcp.tool()
def set_coach_note(text: str, kind: str = "checkin") -> dict[str, Any]:
    """Post the coach's current note to the athlete's dashboard page (replaces the previous note). Keep it short:
    the verdict and the 1-3 things that matter now, e.g. after a check-in, session review, week review or plan
    change. kind: checkin | session-review | week-review | plan. Plain text; line breaks are kept."""
    return dashboard.set_note(text, kind)


@mcp.tool()
def recompute_metrics(full: bool = False) -> dict[str, Any]:
    """Recompute stored activity metrics after threshold changes. full=True re-parses every FIT file so
    time-in-zone also updates (slow for large histories)."""
    return coach.recompute(full)


# ---------------------------------------------------------------- COROS only: calendar and raw access

if _PROVIDER == "coros":
    from . import coros_api

    @mcp.tool()
    def coros_workout_schema() -> dict[str, Any]:
        """The COROS structured-workout format (the `course` object) with its rules. Read it before the first
        push_session_to_coros in a conversation. Only run and bike workouts are supported (no swim)."""
        tools = {t["name"]: t for t in coros_api.list_tools()}
        t = tools["createScheduledWorkout"]
        return {"description": t.get("description"),
                "course_schema": (t.get("inputSchema") or {}).get("properties", {}).get("course")}

    @mcp.tool()
    def push_session_to_coros(date: str, course: dict[str, Any], slot: int = 1) -> dict[str, Any]:
        """Put a planned run or bike session on the athlete's COROS calendar so it syncs to the watch. `course`
        follows coros_workout_schema. The first push creates the workout; pushing the same date/slot again updates
        it in place. Only push after the athlete agreed. COROS cannot move or delete workouts via the API."""
        session = plan.session(date, slot)
        if session is None:
            return {"error": f"No planned session on {date} slot {slot}. Save it with save_plan first."}
        if session.get("sport") not in ("run", "bike", "brick"):
            return {"error": f"COROS only accepts run and bike workouts, not {session.get('sport')}."}
        day = to_date(date).strftime("%Y%m%d")
        if session.get("coros_ref"):
            text = coros_api.call("updateScheduledWorkout", date=day, idInPlan=session["coros_ref"], course=course)
            return {"updated": True, "coros": text[:2000]}
        text = coros_api.call("createScheduledWorkout", date=day, course=course)
        ref = re.search(r"idInPlan\W{0,5}([\w-]+)", text)
        if ref:
            plan.set_coros_ref(date, slot, ref.group(1))
        return {"created": True, "coros_ref_saved": bool(ref), "coros": text[:2000]}

    @mcp.tool()
    def coros_raw(tool: str, arguments: dict[str, Any] | None = None, max_chars: int = 15000) -> dict[str, Any]:
        """Call a read-only COROS tool directly for data the other tools don't cover, e.g. queryActivityLapData,
        queryTrainingSchedule, queryWorkoutLibrary, queryHealthCheckTimeSeries, queryStressTimeSeries, querySleepHrv.
        Dates are yyyyMMdd strings in startDate/endDate, except queryMenstruationCycles, which takes integer
        startDay/endDay (both optional). Writes go through push_session_to_coros only."""
        if not tool.startswith(("query", "get")):
            return {"error": "Only read tools (query*/get*) are allowed here."}
        text = coros_api.call(tool, **(arguments or {}))
        return {"tool": tool, "truncated": len(text) > max_chars, "text": text[:max_chars]}


# ---------------------------------------------------------------- Garmin only: raw access

if _PROVIDER == "garmin":
    from . import garmin_api

    _RAW_RANGE = {
        "daily_summaries": "get_daily_summaries", "hrv_summaries": "get_daily_hrv_summaries",
        "sleep_summaries": "get_daily_sleep_summaries", "body_battery": "get_daily_body_battery",
        "stress": "get_daily_stress", "steps": "get_daily_steps", "weight": "get_weight_measurements",
        "hydration": "get_daily_hydration", "wellness_activities": "get_wellness_activity_summaries",
    }
    _RAW_DAY = {
        "sleep": "get_daily_sleep", "hrv": "get_daily_hrv", "heart_rate": "get_daily_heart_rate",
        "spo2": "get_daily_spo2", "fitness_age": "get_fitness_age", "food_logs": "get_daily_food_logs",
    }
    _RAW_NONE = {"profile": "get_user_profile_settings", "heart_rate_zones": "get_heart_rate_zones"}
    _RAW_ACTIVITY = {"activity": "get_activity", "activity_workouts": "get_activity_workouts"}

    @mcp.tool()
    def garmin_raw(endpoint: str, start_date: str | None = None, end_date: str | None = None,
                   activity_id: int | None = None, max_chars: int = 15000) -> dict[str, Any]:
        """Raw Garmin Personal API response, for data the other tools don't cover or to inspect field names.

        Range endpoints (start_date/end_date): daily_summaries, hrv_summaries, sleep_summaries, body_battery,
        stress, steps, weight, hydration, wellness_activities. Single-day (start_date): sleep, hrv, heart_rate,
        spo2, fitness_age, food_logs. No args: profile, heart_rate_zones. activity_id: activity, activity_workouts.
        """
        start = to_date(start_date)
        end = to_date(end_date) if end_date else start
        if endpoint in _RAW_RANGE:
            data = garmin_api.call(_RAW_RANGE[endpoint], start, end)
        elif endpoint in _RAW_DAY:
            data = garmin_api.call(_RAW_DAY[endpoint], start)
        elif endpoint in _RAW_NONE:
            data = garmin_api.call(_RAW_NONE[endpoint])
        elif endpoint in _RAW_ACTIVITY and activity_id:
            data = garmin_api.call(_RAW_ACTIVITY[endpoint], activity_id)
        else:
            known = sorted({**_RAW_RANGE, **_RAW_DAY, **_RAW_NONE, **_RAW_ACTIVITY})
            return {"error": f"Unknown endpoint or missing activity_id. Known: {known}"}
        text = json.dumps(data, default=str)
        return {"endpoint": endpoint, "truncated": len(text) > max_chars, "json": text[:max_chars]}


def main() -> None:
    mcp.run("stdio")
