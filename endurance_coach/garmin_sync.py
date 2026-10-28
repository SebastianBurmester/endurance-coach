"""Pull data from the Garmin Personal API into the local cache."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from garmin_personal_api import HttpError

from . import coach, fitfile, store
from . import garmin_api as api
from .config import FIT_DIR, FIT_SPORTS, GARMIN_FIT_PER_RUN, GARMIN_HEALTH_SOURCES, MMP_DURATIONS, garmin_sport_of
from .util import deep_get, iter_dated, num, rnd, to_date

_LOGGER = logging.getLogger(__name__)
_PAGE = 50

_HEALTH_METHODS = {
    "hrv": "get_daily_hrv_summaries",
    "daily": "get_daily_summaries",
    "sleep": "get_daily_sleep_summaries",
    "body_battery": "get_daily_body_battery",
    "stress": "get_daily_stress",
    "weight": "get_weight_measurements",
}


# ---------------------------------------------------------------- activities

def _activity_items(page: Any) -> list[dict]:
    if isinstance(page, list):
        return page
    if isinstance(page, dict):
        for value in page.values():
            if isinstance(value, list) and value and isinstance(value[0], dict) and "activityId" in value[0]:
                return value
    return []


def normalize(a: dict[str, Any]) -> dict[str, Any]:
    """A Garmin activity summary in the shared summary fields (the ones the COROS parser produces)."""
    moving = num(a.get("movingDuration")) or num(a.get("duration"))
    dist = num(a.get("distance"))
    speed = num(a.get("averageSpeed"))
    rpe = num(a.get("directWorkoutRpe"))
    s = {
        "duration_s": moving,
        "elapsed_s": num(a.get("elapsedDuration")) or num(a.get("duration")),
        "distance_m": dist,
        "avg_speed_kmh": round(speed * 3.6, 1) if speed else None,
        "moving_avg_speed_kmh": round(dist / moving * 3.6, 1) if dist and moving else None,
        "avg_hr": num(a.get("averageHR")),
        "max_hr": num(a.get("maxHR")),
        "avg_power": num(a.get("avgPower")),
        "np": num(a.get("normPower")),
        "max_power": num(a.get("maxPower")),
        "avg_cadence": num(a.get("averageBikingCadenceInRevPerMinute") or a.get("averageRunningCadenceInStepsPerMinute")),
        "elevation_gain_m": num(a.get("elevationGain")),
        "calories": num(a.get("calories")),
        "training_load": rnd(a.get("activityTrainingLoad"), 1),
        "aerobic_te": rnd(a.get("aerobicTrainingEffect"), 1),
        "anaerobic_te": rnd(a.get("anaerobicTrainingEffect"), 1),
        "training_focus": a.get("trainingEffectLabel"),
        "device_tss": num(a.get("trainingStressScore")),
        "vo2max": num(a.get("vO2MaxValue")),
        "perceived_effort": round(rpe / 10) if rpe else None,  # Garmin stores RPE x10
        "note": a.get("description"),
        "location": a.get("locationName"),
        "mmp": {str(d): v for d in MMP_DURATIONS if (v := num(a.get(f"maxAvgPower_{d}")))} or None,
    }
    return {k: v for k, v in s.items() if v is not None}


def activity_row(a: dict[str, Any]) -> dict[str, Any]:
    start_local = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
    type_key = deep_get(a.get("activityType"), "typeKey") or a.get("activityType")
    type_key = type_key if isinstance(type_key, str) else None
    sport = garmin_sport_of(type_key)
    return {
        "id": int(a["activityId"]),
        "date": start_local[:10],
        "start_local": start_local.replace(" ", "T")[:19],
        "sport": sport,
        "sport_type": None,
        "type": type_key,
        "name": a.get("activityName"),
        "fit": sport in FIT_SPORTS,
        "summary": normalize(a),
    }


def sync_activities(days_back: int | None = None, max_fit_downloads: int = GARMIN_FIT_PER_RUN) -> dict[str, Any]:
    today = date.today()
    last = store.get_meta("activities_synced_through")
    if days_back is not None:
        since = today - timedelta(days=days_back)
    elif last:
        since = to_date(last) - timedelta(days=3)
    else:
        since = today - timedelta(days=365)

    new = seen = 0
    with store.connect() as conn:
        start = 0
        while True:
            items = _activity_items(api.call("search_activities", start=start, limit=_PAGE, start_date=since, end_date=today))
            for a in items:
                if "activityId" not in a:
                    continue
                row = activity_row(a)
                is_new = store.upsert_activity(conn, row)
                new += is_new
                seen += 1
                current = conn.execute("SELECT fit_status FROM activities WHERE id=?", (row["id"],)).fetchone()
                if current["fit_status"] != "ok":
                    store.set_activity_metrics(conn, row["id"], coach.summary_only_metrics(row))
            if len(items) < _PAGE:
                break
            start += _PAGE
    store.set_meta("activities_synced_through", today.isoformat())
    fits = process_pending_fits(max_fit_downloads)
    return {"window": f"{since}..{today}", "activities_seen": seen, "new": new, **fits}


def process_pending_fits(limit: int) -> dict[str, Any]:
    """Download and analyze original FIT files, newest first."""
    FIT_DIR.mkdir(parents=True, exist_ok=True)
    with store.connect() as conn:
        pending = [r["id"] for r in conn.execute(
            "SELECT id FROM activities WHERE fit_status='pending' ORDER BY start_local DESC LIMIT ?", (limit,))]
    done = missing = errors = 0
    for activity_id in pending:
        a = store.activity(activity_id)
        status = "ok"
        try:
            data = fitfile.fit_bytes_from_download(api.call("download_activity", activity_id))
            if data is None:
                status = "missing"
            else:
                (FIT_DIR / f"{activity_id}.fit").write_bytes(data)
                metrics = coach.compute_metrics(a, fitfile.parse(data))
        except api.GarminAuthError:
            raise
        except HttpError as exc:
            status = "missing" if exc.status_code in (403, 404) else "error"
            _LOGGER.warning("FIT download failed for %s: %s", activity_id, exc)
        except Exception:  # noqa: BLE001 - one bad file must not stop the sync
            status = "error"
            _LOGGER.exception("FIT parse failed for %s", activity_id)
        if status != "ok":
            metrics = coach.summary_only_metrics(a)
        with store.connect() as conn:
            store.set_activity_metrics(conn, activity_id, metrics, fit_status=status)
        done += status == "ok"
        missing += status == "missing"
        errors += status == "error"
    with store.connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM activities WHERE fit_status='pending'").fetchone()[0]
    return {"fit_analyzed": done, "fit_missing": missing, "fit_errors": errors, "fit_pending_remaining": remaining}


# ---------------------------------------------------------------- health

def _fetch_window(method: str, start: date, end: date) -> list[Any]:
    """Fetch [start, end]; on a 400 (range too large) split the window in half and retry."""
    try:
        return [api.call(method, start, end)]
    except HttpError as exc:
        if exc.status_code in (400, 413, 422) and start < end:
            mid = start + (end - start) // 2
            return _fetch_window(method, start, mid) + _fetch_window(method, mid + timedelta(days=1), end)
        raise


def sync_health(days_back: int | None = None, sources: list[str] | None = None) -> dict[str, Any]:
    today = date.today()
    report: dict[str, Any] = {}
    for source, (chunk, backfill) in GARMIN_HEALTH_SOURCES.items():
        if sources and source not in sources:
            continue
        first, last = store.daily_date_span(source)
        horizon = today - timedelta(days=backfill)
        if days_back is not None:
            windows = [(today - timedelta(days=days_back), today)]
        elif last:
            # Refresh the last 2 days (sleep/HRV finalize late) and backfill anything older than what's stored.
            windows = [(to_date(last) - timedelta(days=2), today)]
            # Backfill once per horizon; days Garmin has no data for would otherwise be re-requested every run.
            done_from = store.get_meta(f"backfilled_from:{source}")
            if to_date(first) > horizon and (done_from is None or done_from > horizon.isoformat()):
                windows.append((horizon, to_date(first) - timedelta(days=1)))
        else:
            windows = [(horizon, today)]
        days = 0
        unparsed = 0
        try:
            for start, end in windows:
                s = start
                while s <= end:
                    e = min(s + timedelta(days=chunk - 1), end)
                    for payload in _fetch_window(_HEALTH_METHODS[source], s, e):
                        grouped: dict[str, list[dict]] = {}
                        for day, item in iter_dated(payload):
                            grouped.setdefault(day, []).append(item)
                        if not grouped and payload:
                            unparsed += 1
                        with store.connect() as conn:
                            for day, items in grouped.items():
                                store.put_daily(conn, source, day, items[0] if len(items) == 1 else {"items": items})
                        days += len(grouped)
                    s = e + timedelta(days=1)
            if any(w[0] <= horizon for w in windows):
                store.set_meta(f"backfilled_from:{source}", horizon.isoformat())
            report[source] = {"days": days} | ({"unparsed_payloads": unparsed} if unparsed else {})
        except api.GarminAuthError:
            raise
        except HttpError as exc:
            report[source] = {"days": days, "error": f"HTTP {exc.status_code}"}
        except Exception as exc:  # noqa: BLE001
            report[source] = {"days": days, "error": str(exc)[:200]}

    try:
        store.set_meta("garmin_profile", json.dumps(api.call("get_user_profile_settings")))
    except api.GarminAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        report["profile"] = {"error": str(exc)[:200]}
    return report
