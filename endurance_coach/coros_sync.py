"""Pull data from the COROS MCP server into the local cache."""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from . import coach, fitfile, store
from . import coros_api as coros
from . import coros_parse as parse
from .config import FIT_DAILY_LIMIT, FIT_DIR, FIT_SPORTS, HEALTH_BACKFILL_DAYS, coros_sport_of
from .util import to_date

_LOGGER = logging.getLogger(__name__)
_RECORDS_CHUNK_DAYS = 31
_RANGE_CHUNK_DAYS = 31


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


# ---------------------------------------------------------------- activities

def _activity_row(rec: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    sport = coros_sport_of(rec["sport_type"])
    start = datetime.fromtimestamp(rec["start_ts"]).isoformat(timespec="seconds") if rec.get("start_ts") else rec["date"]
    if not start.startswith(rec["date"]):
        start = rec["date"] + "T12:00:00"  # COROS's date is the athlete's local day; trust it over our timezone
    return {
        "id": rec["label_id"],
        "date": rec["date"],
        "start_local": start,
        "sport": sport,
        "sport_type": rec["sport_type"],
        "type": rec["type_name"],
        "name": rec.get("location") or rec["type_name"],
        "fit": sport in FIT_SPORTS,
        "summary": {**rec, **{k: v for k, v in detail.items() if v is not None}},
    }


def sync_activities(days_back: int | None = None, max_fit_downloads: int = FIT_DAILY_LIMIT) -> dict[str, Any]:
    today = date.today()
    last = store.get_meta("activities_synced_through")
    if days_back is not None:
        since = today - timedelta(days=days_back)
    elif last:
        since = to_date(last) - timedelta(days=3)
    else:
        since = today - timedelta(days=365)

    new = seen = 0
    start = since
    while start <= today:
        end = min(start + timedelta(days=_RECORDS_CHUNK_DAYS - 1), today)
        records = parse.sport_records(coros.call("querySportRecords", startDate=_ymd(start), endDate=_ymd(end), limit=200))
        for rec in records:
            existing = store.activity(rec["label_id"])
            detail = {}
            # Detail (COROS load, TE, note, elevation) once per activity, and again for the last 2 days (notes get added).
            if existing is None or rec["date"] >= (today - timedelta(days=2)).isoformat():
                try:
                    detail = parse.activity_detail(coros.call("getActivityDetail", labelId=str(rec["label_id"]),
                                                              sportType=rec["sport_type"]))
                except coros.CorosError as exc:
                    _LOGGER.warning("detail failed for %s: %s", rec["label_id"], exc)
            elif existing:
                detail = {k: v for k, v in existing["summary"].items() if k not in rec}
            row = _activity_row(rec, detail)
            with store.connect() as conn:
                is_new = store.upsert_activity(conn, row)
                current = conn.execute("SELECT fit_status FROM activities WHERE id=?", (row["id"],)).fetchone()
                if current["fit_status"] != "ok":
                    store.set_activity_metrics(conn, row["id"], coach.summary_only_metrics(store_row(row)))
            new += is_new
            seen += 1
        start = end + timedelta(days=1)
    store.set_meta("activities_synced_through", today.isoformat())
    fits = process_pending_fits(max_fit_downloads)
    return {"window": f"{since}..{today}", "activities_seen": seen, "new": new, **fits}


def store_row(row: dict[str, Any]) -> dict[str, Any]:
    """The shape store.activity() returns, for metrics computed before the row is read back."""
    return {"id": row["id"], "date": row["date"], "sport": row["sport"], "type": row["type"], "summary": row["summary"]}


def _fit_budget() -> tuple[str, int]:
    key = f"fit_downloads:{date.today().isoformat()}"
    return key, int(store.get_meta(key) or 0)


def process_pending_fits(limit: int) -> dict[str, Any]:
    """Download and analyze original FIT files, newest first, within COROS's daily FIT allowance."""
    FIT_DIR.mkdir(parents=True, exist_ok=True)
    budget_key, used = _fit_budget()
    limit = max(0, min(limit, FIT_DAILY_LIMIT - used))
    with store.connect() as conn:
        pending = [r["id"] for r in conn.execute(
            "SELECT id FROM activities WHERE fit_status='pending' ORDER BY start_local DESC LIMIT ?", (limit,))]
    done = missing = errors = 0
    for activity_id in pending:
        a = store.activity(activity_id)
        status = "ok"
        try:
            urls = parse.fit_urls(coros.call("queryActivityFitFileDownloadUrls", labelId=str(activity_id),
                                             sportType=a["sport_type"]))
            used += 1
            store.set_meta(budget_key, str(used))
            url = urls.get(activity_id) or next(iter(urls.values()), None)
            data = fitfile.fit_bytes_from_download(coros.download(url)) if url else None
            if data is None:
                status = "missing"
            else:
                (FIT_DIR / f"{activity_id}.fit").write_bytes(data)
                metrics = coach.compute_metrics(a, fitfile.parse(data))
        except coros.CorosAuthError:
            raise
        except coros.CorosError as exc:
            if "daily limit" in str(exc).lower():
                # COROS counts 50 FIT files over a rolling window: leave the rest pending for a later run.
                store.set_meta(budget_key, str(FIT_DAILY_LIMIT))
                break
            status = "error"
            _LOGGER.warning("FIT download failed for %s: %s", activity_id, exc)
        except Exception:  # noqa: BLE001 - one bad file must not stop the sync
            status = "error"
            _LOGGER.exception("FIT download/parse failed for %s", activity_id)
        if status != "ok":
            metrics = coach.summary_only_metrics(a)
        with store.connect() as conn:
            store.set_activity_metrics(conn, activity_id, metrics, fit_status=status)
        done += status == "ok"
        missing += status == "missing"
        errors += status == "error"
    with store.connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM activities WHERE fit_status='pending'").fetchone()[0]
    return {"fit_analyzed": done, "fit_missing": missing, "fit_errors": errors, "fit_pending_remaining": remaining,
            "fit_downloads_today": used}


# ---------------------------------------------------------------- health

def _window(source: str, days_back: int | None) -> tuple[date, date]:
    today = date.today()
    if days_back is not None:
        return today - timedelta(days=days_back), today
    _, last = store.daily_date_span(source)
    if last:
        return to_date(last) - timedelta(days=2), today  # sleep/HRV finalize late
    return today - timedelta(days=HEALTH_BACKFILL_DAYS[source] - 1), today


def _ranged(tool: str, parser: Any, start: date, end: date) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    s = start
    while s <= end:
        e = min(s + timedelta(days=_RANGE_CHUNK_DAYS - 1), end)
        out |= parser(coros.call(tool, startDate=_ymd(s), endDate=_ymd(e)))
        s = e + timedelta(days=1)
    return out


def _by_days(tool: str, parser: Any, start: date, end: date, cap: int | None = None) -> dict[str, dict[str, Any]]:
    days = (end - start).days + 1
    return parser(coros.call(tool, days=min(days, cap) if cap else days))


_HEALTH = {
    "rhr": lambda s, e: _by_days("queryRestingHeartRate", parse.resting_hr, s, e),
    "hrv": lambda s, e: _ranged("querySleepHrv", parse.sleep_hrv, s, e),
    "sleep": lambda s, e: _ranged("querySleepOverview", parse.sleep_overview, s, e),
    "daily": lambda s, e: _by_days("queryDailyHealthData", parse.daily_health, s, e),
    "coros_load": lambda s, e: _by_days("queryTrainingLoadAssessment", parse.training_load, s, e, cap=30),
}


def sync_health(days_back: int | None = None) -> dict[str, Any]:
    today = date.today()
    report: dict[str, Any] = {}
    for source, fetch in _HEALTH.items():
        start, end = _window(source, days_back)
        try:
            records = fetch(start, end)
            with store.connect() as conn:
                for day, rec in records.items():
                    if start.isoformat() <= day <= end.isoformat():
                        store.put_daily(conn, source, day, rec)
            report[source] = {"days": len(records)}
        except coros.CorosAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            report[source] = {"error": str(exc)[:200]}

    # Snapshots that only exist for "now": recovery (stored for today), fitness assessment and profile.
    for name, tool, parser in (("recovery", "queryRecoveryStatus", parse.recovery),
                               ("coros_fitness", "queryFitnessAssessmentOverview", parse.fitness),
                               ("coros_user", "queryUserInfo", parse.user_info)):
        try:
            value = parser(coros.call(tool))
            if name == "recovery":
                with store.connect() as conn:
                    store.put_daily(conn, "recovery", today.isoformat(), value | {"at": time.strftime("%H:%M")})
            else:
                store.set_meta(name, json.dumps(value))
        except coros.CorosAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            report[name] = {"error": str(exc)[:200]}
    return report
