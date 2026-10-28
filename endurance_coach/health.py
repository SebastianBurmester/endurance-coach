"""Per-day health records from the synced sources (Garmin or COROS), and readiness against personal baselines."""

from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from typing import Any

from . import settings, store
from .util import date_range, deep_get, num, rnd


def _bb_at_wake(bb: Any, sleep: Any) -> float | None:
    """Body Battery level at the sample closest to sleep end (within 90 min).

    The body-battery report has bodyBatteryValuesArray rows like [timestamp_ms, level], described by
    bodyBatteryValueDescriptorDTOList.
    """
    wake_ms = num(deep_get(sleep, "sleepEndTimestampGMT"))
    if not isinstance(bb, dict) or not wake_ms:
        return None
    ts_i, lvl_i = 0, 1
    for d in bb.get("bodyBatteryValueDescriptorDTOList") or []:
        key = str(d.get("bodyBatteryValueDescriptorKey", "")).lower()
        if key == "timestamp":
            ts_i = d.get("bodyBatteryValueDescriptorIndex", ts_i)
        elif "level" in key:
            lvl_i = d.get("bodyBatteryValueDescriptorIndex", lvl_i)
    points = [(p[ts_i], p[lvl_i]) for p in bb.get("bodyBatteryValuesArray") or []
              if isinstance(p, list) and len(p) > max(ts_i, lvl_i) and p[lvl_i] is not None]
    if not points:
        return None
    ts, level = min(points, key=lambda p: abs(p[0] - wake_ms))
    return float(level) if abs(ts - wake_ms) <= 90 * 60 * 1000 else None


def _garmin_day(day: str, src: dict[str, Any]) -> dict[str, Any]:
    """Raw Garmin payloads (read defensively, field names vary by endpoint) in the shared record fields."""
    hrv, daily, sleep = src.get("hrv"), src.get("daily"), src.get("sleep")
    bb, stress, weight = src.get("body_battery"), src.get("stress"), src.get("weight")

    weight_value = num(deep_get(weight, "weight"))
    if weight_value and weight_value > 500:  # Garmin reports grams
        weight_value /= 1000
    sleep_s = num(deep_get(sleep, "sleepTimeSeconds", "totalSleepSeconds", "sleepDurationInSeconds"))
    return {
        "hrv": num(deep_get(hrv, "lastNightAvg") or deep_get(sleep, "avgOvernightHrv")),
        "hrv_weekly": num(deep_get(hrv, "weeklyAvg")),
        "hrv_baseline_low": num(deep_get(hrv, "baseline.balancedLow", "balancedLow")),
        "hrv_baseline_high": num(deep_get(hrv, "baseline.balancedUpper", "balancedUpper")),
        "hrv_status": deep_get(hrv, "status"),
        "rhr": num(deep_get(daily, "heartRate.restingValue", "restingHeartRate", "restingHeartRateValue")
                   or deep_get(sleep, "restingHeartRate")),
        "sleep_h": round(sleep_s / 3600, 2) if sleep_s else None,
        "sleep_score": num(deep_get(sleep, "sleepScores.overall.value", "overallSleepScore", "sleepScore")),
        "deep_min": rnd((num(deep_get(sleep, "deepSleepSeconds")) or 0) / 60) or None,
        "rem_min": rnd((num(deep_get(sleep, "remSleepSeconds")) or 0) / 60) or None,
        "bb_wake": num(deep_get(daily, "bodyBatteryAtWakeTime")) or _bb_at_wake(bb, sleep),
        "bb_high": num(deep_get(daily, "bodyBattery.maxValue", "bodyBatteryHighestValue")),
        "bb_low": num(deep_get(daily, "bodyBattery.minValue", "bodyBatteryLowestValue")),
        "bb_charged": num(deep_get(bb, "charged") or deep_get(daily, "bodyBattery.chargedValue", "bodyBatteryChargedValue")),
        "stress_avg": num(deep_get(stress, "overallStressLevel", "averageStressLevel", "avgStressLevel")
                          or deep_get(daily, "stress.avgLevel", "averageStressLevel")),
        "steps": num(deep_get(daily, "movement.steps.value", "totalSteps")),
        "weight_kg": round(weight_value, 1) if weight_value else None,
    }


def _coros_day(day: str, src: dict[str, Any]) -> dict[str, Any]:
    """Parsed COROS records (see coros_parse.py) in the shared record fields."""
    hrv, rhr, sleep = src.get("hrv") or {}, src.get("rhr") or {}, src.get("sleep") or {}
    daily, load, rec = src.get("daily") or {}, src.get("coros_load") or {}, src.get("recovery") or {}
    sleep_min = sleep.get("main_sleep_min") or daily.get("sleep_total_min")
    deep, rem = sleep.get("deep_pct"), sleep.get("rem_pct")
    return {
        "hrv": hrv.get("hrv"),
        "hrv_status": hrv.get("evaluation"),
        "hrv_baseline_low": hrv.get("normal_low"),
        "hrv_baseline_high": hrv.get("normal_high"),
        "hrv_baseline": hrv.get("baseline"),
        "rhr": rhr.get("rhr"),
        "sleep_h": round(sleep_min / 60, 2) if sleep_min else None,
        "sleep_score": sleep.get("sleep_score"),
        "deep_min": rnd(sleep_min * deep / 100) if sleep_min and deep is not None else None,
        "rem_min": rnd(sleep_min * rem / 100) if sleep_min and rem is not None else None,
        "sleep_hr_min": daily.get("sleep_hr_min"),
        "stress_avg": daily.get("stress_avg"),
        "steps": daily.get("steps"),
        "recovery_pct": rec.get("recovery_pct"),
        "recovery_level": rec.get("level"),
        "coros_load_ratio": load.get("ratio"),
        "coros_load_comment": load.get("comment"),
        "coros_short_term": load.get("short_term"),
        "coros_long_term": load.get("long_term"),
    }


# Every field a day can have, whichever source it came from (absent ones are None).
_FIELDS = ("hrv", "hrv_weekly", "hrv_status", "hrv_baseline_low", "hrv_baseline_high", "hrv_baseline", "rhr", "sleep_h",
           "sleep_score", "deep_min", "rem_min", "sleep_hr_min", "stress_avg", "steps", "weight_kg",
           "bb_wake", "bb_high", "bb_low", "bb_charged", "recovery_pct", "recovery_level",
           "coros_load_ratio", "coros_load_comment", "coros_short_term", "coros_long_term")


def day_record(day: str, src: dict[str, Any], provider: str | None = None) -> dict[str, Any]:
    rec = (_coros_day if (provider or settings.provider()) == "coros" else _garmin_day)(day, src)
    return {"date": day, **{k: rec.get(k) for k in _FIELDS}}


def rows(start: date, end: date) -> list[dict[str, Any]]:
    raw = store.daily(start.isoformat(), end.isoformat())
    provider = settings.provider()
    return [day_record(d.isoformat(), raw.get(d.isoformat(), {}), provider) for d in date_range(start, end)]


def latest_weight() -> float | None:
    raw = store.get_meta("coros_user")
    if raw:
        return json.loads(raw).get("weight_kg")
    end = date.today()
    for r in reversed(rows(end - timedelta(days=60), end)):
        if r["weight_kg"]:
            return r["weight_kg"]
    return None


def _series(records: list[dict], key: str) -> list[float]:
    return [r[key] for r in records if isinstance(r.get(key), (int, float))]


def _stats(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 5:
        return (statistics.fmean(values) if values else None), None
    return statistics.fmean(values), statistics.stdev(values)


def readiness(day: date, load: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compare last night / today against 7-day and 60-day personal baselines.

    Garmin and COROS both date sleep and overnight HRV by wake-up day, so `day` covers the night before it.
    """
    provider = settings.provider()
    watch = settings.provider_label(provider)
    history = rows(day - timedelta(days=60), day)
    today = history[-1]
    prior = history[:-1]
    last7 = history[-7:]
    flags: list[str] = []
    positives: list[str] = []

    # HRV: 7-day rolling mean vs 60-day normal range (mean ± 0.75 SD), plus last night vs normal.
    hrv60_mean, hrv60_sd = _stats(_series(prior, "hrv"))
    hrv7 = _series(last7, "hrv")
    hrv: dict[str, Any] = {"last_night": today["hrv"], "watch_status": today["hrv_status"],
                           "watch_normal_range": [today["hrv_baseline_low"], today["hrv_baseline_high"]],
                           "watch_baseline": today["hrv_baseline"] or today["hrv_weekly"]}
    if hrv7:
        hrv["avg_7d"] = rnd(statistics.fmean(hrv7), 1)
    if hrv60_mean and hrv60_sd:
        low, high = hrv60_mean - 0.75 * hrv60_sd, hrv60_mean + 0.75 * hrv60_sd
        hrv["normal_range_60d"] = [rnd(low, 1), rnd(high, 1)]
        if hrv.get("avg_7d") is not None and hrv["avg_7d"] < low:
            flags.append(f"HRV 7-day avg {hrv['avg_7d']} below normal range ({low:.0f}-{high:.0f}): accumulated stress")
        if today["hrv"] and today["hrv"] < hrv60_mean - hrv60_sd:
            flags.append(f"Last-night HRV {today['hrv']:.0f} more than 1 SD below 60-day mean ({hrv60_mean:.0f})")
        elif today["hrv"] and today["hrv"] >= hrv60_mean:
            positives.append("HRV at or above baseline")
        if len(hrv7) >= 5:
            hrv["cv_7d_pct"] = rnd(statistics.stdev(hrv7) / statistics.fmean(hrv7) * 100, 1)
    if provider == "coros":
        below = [r for r in last7[-3:] if isinstance(r["hrv_status"], str) and r["hrv_status"].lower().startswith("below")]
        if len(below) >= 2:
            flags.append(f"COROS HRV 'below normal' on {len(below)} of the last 3 nights")
    elif isinstance(today["hrv_status"], str) and today["hrv_status"].upper() in {"UNBALANCED", "LOW", "POOR"}:
        flags.append(f"Garmin HRV status {today['hrv_status']}")

    # Resting HR vs 30-day mean.
    rhr30, _ = _stats(_series(prior[-30:], "rhr"))
    rhr: dict[str, Any] = {"today": today["rhr"], "avg_30d": rnd(rhr30, 1)}
    if today["rhr"] and rhr30:
        delta = today["rhr"] - rhr30
        rhr["delta"] = rnd(delta, 1)
        if delta >= 5:
            flags.append(f"Resting HR +{delta:.0f} bpm vs 30-day avg (fatigue, illness or heat/alcohol)")
        elif delta <= -2:
            positives.append("Resting HR below average")

    # Sleep.
    sleep7 = _series(last7, "sleep_h")
    sleep = {"hours": today["sleep_h"], "score": today["sleep_score"], "deep_min": today["deep_min"],
             "rem_min": today["rem_min"], "avg_7d_h": rnd(statistics.fmean(sleep7), 1) if sleep7 else None}
    if today["sleep_h"] and today["sleep_h"] < 6.5:
        flags.append(f"Short sleep ({today['sleep_h']:.1f} h)")
    if today["sleep_score"] and today["sleep_score"] < 60:
        flags.append(f"Poor sleep score ({today['sleep_score']:.0f})")
    if sleep["avg_7d_h"] and sleep["avg_7d_h"] < 7:
        flags.append(f"7-day sleep average {sleep['avg_7d_h']} h: sleep debt")

    # The watch's own recovery gauge: COROS recovery % (a snapshot taken at sync time) or Garmin Body Battery.
    if provider == "coros":
        recovery = {"source": "COROS recovery", "pct": today["recovery_pct"], "level": today["recovery_level"],
                    "coros_load_ratio": today["coros_load_ratio"], "coros_load_comment": today["coros_load_comment"]}
        if today["recovery_pct"] is not None and today["recovery_pct"] < 50:
            flags.append(f"COROS recovery only {today['recovery_pct']:.0f}% ({today['recovery_level']})")
        elif today["recovery_pct"] is not None and today["recovery_pct"] >= 85:
            positives.append("COROS recovery high")
    else:
        morning_bb = today["bb_wake"] or today["bb_high"]
        recovery = {"source": "Body Battery", "pct": morning_bb, "wake": today["bb_wake"], "high": today["bb_high"],
                    "charged": today["bb_charged"]}
        if morning_bb and morning_bb < 50:
            flags.append(f"Body Battery only {morning_bb:.0f} at wake")
        elif morning_bb and morning_bb >= 75:
            positives.append("Body Battery well charged")

    yesterday = history[-2] if len(history) > 1 else {}
    if load:
        if (load.get("tsb") or 0) < -30:
            flags.append(f"TSB {load['tsb']}: deep fatigue")
        if (load.get("ramp_7d") or 0) > 8:
            flags.append(f"CTL ramp {load['ramp_7d']}/week: aggressive build")

    major = sum(1 for f in flags if f.startswith(("HRV 7-day", "Last-night HRV", "Resting HR", "TSB")))
    status = "red" if major >= 2 or len(flags) >= 4 else "amber" if flags else "green"
    return {
        "date": day.isoformat(),
        "status": status,
        "flags": flags,
        "positives": positives,
        "hrv": hrv,
        "resting_hr": rhr,
        "sleep": sleep,
        "recovery": recovery,
        "watch": watch,
        "stress_yesterday": yesterday.get("stress_avg"),
        "load": load,
        "data_present": any(v is not None for k, v in today.items() if k != "date"),
    }
