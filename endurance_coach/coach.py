"""Coaching analyses built on the local cache. Every function returns compact JSON-able dicts."""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from . import athlete, health, metrics, plan, settings, store
from .athlete import s_to_pace
from .config import FIT_DIR, MMP_DURATIONS, NON_TRAINING_SPORTS, POWER_ZONES
from .fitfile import Stream, parse
from .util import deep_get, hms, num, rnd, to_date

_LOGGER = logging.getLogger(__name__)

# Always shown in a ride's best efforts; other durations appear only when they set a new 90-day best.
_KEY_DURATIONS = {5, 60, 300, 1200, 3600}
_ENDURANCE_SPORTS = ("swim", "bike", "run")
_LTHR_OF_MAX = 0.89


def _label(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}min" if seconds % 60 == 0 else f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:g}h"


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != {} and v != []}


def _hours(a: dict[str, Any]) -> float:
    return (num(a["summary"].get("duration_s")) or num(a["metrics"].get("recorded_s")) or 0) / 3600


def _km(a: dict[str, Any]) -> float:
    return (num(a["summary"].get("distance_m")) or 0) / 1000


# ---------------------------------------------------------------- thresholds

def meta_json(name: str) -> dict[str, Any]:
    raw = store.get_meta(name)
    return json.loads(raw) if raw else {}


def _source_thresholds() -> dict[str, Any]:
    """What the data source knows: Garmin profile settings, or COROS EvoLab / profile."""
    if settings.provider() == "coros":
        fitness, user = meta_json("coros_fitness"), meta_json("coros_user")
        return {"run_threshold_s_km": num(fitness.get("threshold_pace_s_km")), "weight_kg": num(user.get("weight_kg")),
                "label": "COROS EvoLab"}
    gp = meta_json("garmin_profile")
    weight = num(deep_get(gp, "weight"))
    return {"ftp": num(deep_get(gp, "functionalThresholdPower", "ftp")),
            "lthr": num(deep_get(gp, "lactateThresholdHeartRate")), "max_hr": num(deep_get(gp, "maxHeartRate", "maxHr")),
            "weight_kg": weight / 1000 if weight and weight > 500 else weight, "label": "Garmin profile"}


def _recent_device(day: str | date, field: str) -> float | None:
    """A threshold the watch stored in the FIT of the latest analyzed activity on/before `day`."""
    until = day.isoformat() if isinstance(day, date) else str(day)[:10]
    since = (to_date(until) - timedelta(days=120)).isoformat()
    for a in reversed(store.activities(since=since, until=until)):
        if a["metrics"].get(field):
            return float(a["metrics"][field])
    return None


def thresholds(day: str | date, stream: Stream | None = None) -> dict[str, Any]:
    """Per-sport thresholds: athlete.json > values the watch stored in the FIT > the data source's profile."""
    profile = athlete.load()
    src = _source_thresholds()

    ftp, ftp_source = athlete.value_on("ftp_history", day, profile), "athlete.json"
    if not ftp:
        ftp = (num(stream.device_ftp) if stream else None) or _recent_device(day, "device_ftp")
        ftp_source = "watch (FIT)" if ftp else None
    if not ftp and src.get("ftp"):
        ftp, ftp_source = src["ftp"], src["label"]

    run_thr, run_source = athlete.value_on("run_threshold_history", day, profile), "athlete.json"
    if not run_thr and src.get("run_threshold_s_km"):
        run_thr, run_source = src["run_threshold_s_km"], src["label"]
    if not run_thr:
        run_source = None

    css = athlete.value_on("css_history", day, profile)
    max_hr = (num(profile.get("max_hr")) or (num(stream.device_max_hr) if stream else None)
              or _recent_device(day, "device_max_hr") or src.get("max_hr"))
    # Garmin's profile LTHR comes from running; metrics.lthr_for derives the bike value when only one is known.
    lthr_bike = num(profile.get("lthr_bike"))
    lthr_run = num(profile.get("lthr_run")) or (None if lthr_bike else src.get("lthr"))
    weight = num(profile.get("weight_kg")) or health.latest_weight() or src.get("weight_kg")
    return {
        "ftp": ftp, "ftp_source": ftp_source,
        "run_threshold_s_km": run_thr, "run_threshold_pace": s_to_pace(run_thr), "run_threshold_source": run_source,
        "css_s_100m": css, "css_pace": s_to_pace(css), "css_source": "athlete.json" if css else None,
        "lthr_bike": lthr_bike, "lthr_run": lthr_run,
        # Until an LTHR is known, ~89% of max HR stands in for it (hrTSS marked hr_est, zones marked estimated).
        "lthr_est": rnd(_LTHR_OF_MAX * max_hr) if max_hr and not (lthr_bike or lthr_run) else None,
        "max_hr": max_hr, "weight_kg": weight,
    }


# ---------------------------------------------------------------- per-activity metrics

def compute_metrics(a: dict[str, Any], stream: Stream) -> dict[str, Any]:
    th = thresholds(a["date"], stream)
    m = metrics.stream_metrics(a["sport"], stream, th)
    m["device_ftp"] = stream.device_ftp
    m["device_max_hr"] = stream.device_max_hr
    if "tss" not in m:
        fallback = summary_only_metrics(a, th)
        for key in ("tss", "tss_source", "if", "avg_pace_s_100m"):
            if key in fallback:
                m[key] = fallback[key]
    return _clean(m)


def summary_only_metrics(a: dict[str, Any], th: dict[str, Any] | None = None) -> dict[str, Any]:
    m = metrics.summary_metrics(a["sport"], a["summary"], th or thresholds(a["date"]), settings.provider())
    if a["sport"] in NON_TRAINING_SPORTS:
        m["tss"], m["tss_source"] = 0, "excluded"
    return m


def load_stream(activity_id: int) -> Stream | None:
    path = FIT_DIR / f"{activity_id}.fit"
    return parse(path.read_bytes()) if path.exists() else None


def recompute(full: bool = False) -> dict[str, Any]:
    """Re-derive metrics after threshold changes. full=True re-parses every FIT file (also refreshes zones)."""
    counts: dict[str, int] = defaultdict(int)
    for a in store.activities():
        m = a["metrics"]
        stream = load_stream(a["id"]) if a["fit_status"] == "ok" and full else None
        if stream is not None:
            m = compute_metrics(a, stream)
            counts["reparsed"] += 1
        elif a["fit_status"] == "ok":
            th = thresholds(a["date"])
            if a["sport"] == "bike" and m.get("np") and th["ftp"] and m.get("tss_source") in (
                    "power", None, "coros", "garmin", "hr", "hr_est"):
                m |= {"ftp_used": th["ftp"], "if": round(m["np"] / th["ftp"], 2), "tss_source": "power",
                      "tss": rnd(metrics.tss_from_np(m["np"], m["recorded_s"], th["ftp"]))}
            elif a["sport"] == "run" and m.get("ngp_s_km") and th["run_threshold_s_km"]:
                thr, ngp = th["run_threshold_s_km"], m["ngp_s_km"]
                moving = (m.get("avg_pace_s_km") or ngp) * _km(a) or m["recorded_s"]
                m |= {"threshold_used_s_km": thr, "if": round(thr / ngp, 2), "tss_source": "pace",
                      "tss": rnd(metrics.r_tss(moving, ngp, thr))}
            else:
                fallback = summary_only_metrics(a, th)
                m |= {k: fallback[k] for k in ("tss", "tss_source", "if") if k in fallback}
            counts["tss_updated"] += 1
        else:
            m = summary_only_metrics(a)
            counts["summary"] += 1
        with store.connect() as conn:
            store.set_activity_metrics(conn, a["id"], m)
    return dict(counts)


def overview(a: dict[str, Any]) -> dict[str, Any]:
    s, m, sport = a["summary"], a["metrics"], a["sport"]
    dist = num(s.get("distance_m"))
    return _clean({
        "id": a["id"],
        "start": a["start_local"],
        "sport": sport,
        "type": a["type"],
        "name": a["name"],
        "distance_km": rnd(dist / 1000, 2 if sport == "swim" else 1) if dist else None,
        "time": hms(num(s.get("duration_s"))),
        "elevation_m": rnd(s.get("elevation_gain_m")),
        "avg_speed_kmh": rnd(s.get("moving_avg_speed_kmh") or s.get("avg_speed_kmh"), 1) if sport == "bike" else None,
        "pace_km": s_to_pace(m.get("avg_pace_s_km")) if sport == "run" else None,
        "ngp_km": s_to_pace(m.get("ngp_s_km")) if sport == "run" else None,
        "pace_100m": s_to_pace(m.get("avg_pace_s_100m")) if sport == "swim" else None,
        "tss": m.get("tss"),
        "tss_source": m.get("tss_source"),
        "if": m.get("if"),
        "np": m.get("np"),
        "avg_power": m.get("avg_power"),
        "avg_hr": m.get("avg_hr") or rnd(s.get("avg_hr")),
        "ef": m.get("ef"),
        "decoupling_pct": (m.get("decoupling") or {}).get("decoupling_pct"),
        "watch_load": rnd(s.get("training_load")),
        "watch_te": f"{s.get('aerobic_te')}/{s.get('anaerobic_te')}" if s.get("aerobic_te") is not None else None,
        "watch_focus": s.get("training_focus"),
        "rpe": s.get("perceived_effort"),
        "note": s.get("note"),
        "fit": a["fit_status"] if a["fit_status"] != "none" else None,
    })


def list_activities(days: int = 14, sport: str | None = None) -> list[dict[str, Any]]:
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    return [overview(a) for a in reversed(store.activities(since=since, sport=sport))]


# ---------------------------------------------------------------- load

def _daily_tss(since: date) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for a in store.activities(since=since.isoformat()):
        out[a["date"]] += num(a["metrics"].get("tss")) or 0.0
    return out


def pmc_series(start: date, end: date) -> list[dict[str, Any]]:
    return metrics.pmc(_daily_tss(start - timedelta(days=150)), start, end, seed_days=150)


def load_today(day: date | None = None) -> dict[str, Any]:
    day = day or date.today()
    row = pmc_series(day, day)[-1]
    ctl, atl = row["ctl"] or 0, row["atl"] or 0
    return {**row, "acwr": round(atl / ctl, 2) if ctl else None}


def _sport_totals(acts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, float]] = {}
    for a in acts:
        t = out.setdefault(a["sport"], defaultdict(float))
        t["sessions"] += 1
        t["hours"] += _hours(a)
        t["km"] += _km(a)
        t["tss"] += num(a["metrics"].get("tss")) or 0
        if a["sport"] in ("bike", "run"):
            t["elevation_m"] += num(a["summary"].get("elevation_gain_m")) or 0
            t["longest_h"] = max(t["longest_h"], _hours(a))
    return {sport: _clean({"sessions": int(t["sessions"]), "hours": rnd(t["hours"], 1),
                           "km": rnd(t["km"], 2 if sport == "swim" else 0) or None, "tss": rnd(t["tss"]),
                           "elevation_m": rnd(t["elevation_m"]) or None, "longest_h": rnd(t["longest_h"], 1) or None})
            for sport, t in out.items()}


def training_load(days: int = 84) -> dict[str, Any]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    series = pmc_series(start, end)
    acts = store.activities(since=start.isoformat())

    week_start = start - timedelta(days=start.weekday())
    weeks: dict[str, list[dict]] = {}
    w = week_start
    while w <= end:
        weeks[w.isoformat()] = []
        w += timedelta(days=7)
    sources: dict[str, int] = defaultdict(int)
    for a in acts:
        d = to_date(a["date"])
        weeks[(d - timedelta(days=d.weekday())).isoformat()].append(a)
        sources[a["metrics"].get("tss_source") or "none"] += 1

    by_date = {r["date"]: r for r in series}
    weekly = []
    for wk_start, wk_acts in weeks.items():
        wk_end = min(to_date(wk_start) + timedelta(days=6), end).isoformat()
        training = [a for a in wk_acts if a["sport"] not in NON_TRAINING_SPORTS]
        weekly.append(_clean({
            "week_of": wk_start,
            "hours": rnd(sum(_hours(a) for a in training), 1),
            "tss": rnd(sum(num(a["metrics"].get("tss")) or 0 for a in training)),
            "by_sport": _sport_totals(training),
            "ctl_end": by_date.get(wk_end, {}).get("ctl"),
        }))

    current = series[-1]
    peak = max(series, key=lambda r: r["ctl"] or 0)
    coros = settings.provider() == "coros"
    coros_load = health.rows(end - timedelta(days=6), end) if coros else []
    return {
        "today": {**current, "acwr": round(current["atl"] / current["ctl"], 2) if current["ctl"] else None},
        "ctl_peak_in_window": {"date": peak["date"], "ctl": peak["ctl"]},
        "weekly": weekly,
        "last_14_days": series[-14:],
        "tss_sources_in_window": dict(sources),
        "coros_load_last_7d": [_clean({"date": r["date"], "short": r["coros_short_term"], "long": r["coros_long_term"],
                                       "ratio": r["coros_load_ratio"], "comment": r["coros_load_comment"]})
                               for r in coros_load] or None,
        "notes": "CTL/ATL/TSB combine all sports. TSB = form going into the day. ACWR = ATL/CTL (>1.3 spikes "
                 "injury/illness risk)." + (" COROS short/long-term load is COROS's own model, shown for comparison."
                                           if coros else ""),
    }


# ---------------------------------------------------------------- power curve & zones

def _best_efforts(since: date, until: date) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for a in store.activities(since=since.isoformat(), until=until.isoformat(), sport="bike"):
        for d, w in (a["metrics"].get("mmp") or {}).items():
            if w and w > best.get(d, {}).get("w", 0):
                best[d] = {"w": w, "date": a["date"], "activity_id": a["id"]}
    return best


def power_curve(days: int = 90, compare_days: int = 365) -> dict[str, Any]:
    end = date.today()
    current = _best_efforts(end - timedelta(days=days - 1), end)
    reference = _best_efforts(end - timedelta(days=compare_days - 1), end)
    th = thresholds(end)
    rows = []
    for d in MMP_DURATIONS:
        cur = current.get(str(d))
        if not cur:
            continue
        ref = reference.get(str(d))
        rows.append(_clean({
            "duration": _label(d),
            "watts": rnd(cur["w"]),
            "w_kg": round(cur["w"] / th["weight_kg"], 2) if th["weight_kg"] else None,
            "pct_ftp": rnd(100 * cur["w"] / th["ftp"]) if th["ftp"] else None,
            "date": cur["date"],
            f"best_{compare_days}d": rnd(ref["w"]) if ref else None,
            "pct_of_best": rnd(100 * cur["w"] / ref["w"]) if ref else None,
        }))
    out = {
        "window_days": days,
        "thresholds": th,
        "curve": rows,
        "ftp_estimates": metrics.ftp_estimates({k: v["w"] for k, v in current.items()}),
        "notes": "Estimates are only as good as the maximal efforts in the window (rides with power only). "
                 "CP from 3-20 min bests usually sits a few % above a 60-min FTP.",
    }
    if not rows:
        out["empty"] = "No rides with power in the window."
    return out


def zone_distribution(days: int = 28, sport: str | None = None, end: date | None = None) -> dict[str, Any]:
    end = end or date.today()
    since = end - timedelta(days=days - 1)
    hz: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    pz: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for a in store.activities(since=since.isoformat(), until=end.isoformat(), sport=sport):
        if a["sport"] in NON_TRAINING_SPORTS:
            continue
        m = a["metrics"]
        for k, v in (m.get("hr_zones_s") or {}).items():
            hz[a["sport"]][k] += v
        for k, v in (m.get("power_zones_s") or {}).items():
            pz[k] += v
        counts["with_hr_zones" if m.get("hr_zones_s") else "without_zones"] += 1

    def table(z: dict[str, float]) -> list[dict[str, Any]]:
        total = sum(z.values()) or 1
        return [{"zone": k, "time": hms(v), "pct": rnd(100 * v / total, 1)} for k, v in z.items()]

    def three_zone(z: dict[str, float]) -> dict[str, Any]:
        names = list(z)
        total = sum(z.values()) or 1
        # Friel HR zones: Z1-Z2 low, Z3-Z4 moderate, Z5 high.
        low, mid, high = sum(z[n] for n in names[:2]), sum(z[n] for n in names[2:4]), sum(z[n] for n in names[4:])
        out = {"low_pct": rnd(100 * low / total, 1), "mid_pct": rnd(100 * mid / total, 1), "high_pct": rnd(100 * high / total, 1)}
        if low and mid and high:
            # Treff et al. polarization index: >2.0 = polarized.
            out["polarization_index"] = round(math.log10((low / total) / (mid / total) * (high / total) * 100), 2)
        return out

    combined: dict[str, float] = defaultdict(float)
    for z in hz.values():
        for k, v in z.items():
            combined[k[:2]] += v
    result: dict[str, Any] = {"window_days": days, **counts,
                              "hr_zones_by_sport": {s: table(z) for s, z in hz.items()}}
    if combined:
        result["all_sports_three_zone"] = three_zone(combined)
    if pz:
        result["power_zones_bike"] = table(pz)
    if not hz:
        result["note"] = "No HR zones yet: set lthr_bike / lthr_run (or max_hr) in the profile, then recompute_metrics(full=true)."
    return result


# ---------------------------------------------------------------- single session

def analyze_session(activity_id: int | None = None, sport: str | None = None) -> dict[str, Any]:
    a = store.activity(activity_id) if activity_id else store.latest_activity(sport)
    if not a:
        return {"error": "Activity not found in cache. Run sync_data first."}
    out: dict[str, Any] = {"overview": overview(a)}
    stream = load_stream(a["id"]) if a["fit_status"] == "ok" else None
    th = thresholds(a["date"], stream)
    out["thresholds"] = th
    if stream:
        m = compute_metrics(a, stream)
        out["laps"] = metrics.lap_table(stream, a["sport"], th)[:40]
    else:
        m = a["metrics"]
        out["note"] = f"No FIT stream (status {a['fit_status']}); metrics from the {settings.provider_label()} summary only."
    if settings.provider() == "coros" and a["sport"] in ("run", "swim") and len(out.get("laps") or []) <= 1:
        try:
            from . import coros_api, coros_parse
            out["laps"] = coros_parse.laps(coros_api.call("queryActivityLapData", labelId=str(a["id"]),
                                                          sportType=a["sport_type"]))[:40]
            out["laps_source"] = "COROS (pace incl. grade-adjusted, power from the watch)"
        except Exception as exc:  # noqa: BLE001 - laps are optional
            _LOGGER.warning("lap data failed for %s: %s", a["id"], exc)

    for key in ("power_zones_s", "hr_zones_s"):
        if m.get(key):
            total = sum(m[key].values()) or 1
            m[key.replace("_s", "")] = {k: f"{hms(v)} ({100 * v / total:.0f}%)" for k, v in m[key].items()}
            del m[key]

    day = to_date(a["date"])
    if m.get("mmp"):
        prior = _best_efforts(day - timedelta(days=90), day - timedelta(days=1))
        bests = []
        for d in MMP_DURATIONS:
            w = m["mmp"].get(str(d))
            if not w:
                continue
            ref = prior.get(str(d), {}).get("w")
            if d not in _KEY_DURATIONS and not (ref and w > ref):
                continue
            bests.append(_clean({
                "duration": _label(d), "watts": rnd(w),
                "w_kg": round(w / th["weight_kg"], 2) if th["weight_kg"] else None,
                "prior_90d_best": rnd(ref), "new_90d_best": True if ref and w > ref else None,
            }))
        m["best_efforts"] = bests
        del m["mmp"]
    for key in ("avg_pace_s_km", "ngp_s_km", "avg_pace_s_100m"):
        if m.get(key):
            m[key.replace("_s_", "_")] = s_to_pace(m[key])
    out["metrics"] = m
    planned = plan.sessions_on(day)
    out["planned_session"] = planned or "nothing planned for this day"
    if stream and a["sport"] == "bike":
        # Work reps found in the power stream (lap button or not), judged against the planned session's band.
        target = next((p for p in planned if p.get("sport") in ("bike", "brick") and p.get("status") != "skipped"), None)
        intervals = metrics.detect_intervals(stream, th["ftp"], target)
        if intervals and intervals["count"]:
            out["intervals"] = intervals
            if intervals["count"] >= 2 and (m.get("decoupling") or {}).get("decoupling_pct") is not None:
                m["decoupling"]["note"] = ("Whole-ride value mixes work and recovery on an interval day: "
                                           "judge drift per rep (intervals[].pw_hr_drift_pct) instead.")
    out["form_going_in"] = load_today(day)
    s = a["summary"]
    out["watch"] = _clean({
        "source": settings.provider_label(), "device_tss": s.get("device_tss"), "vo2max": s.get("vo2max"),
        "training_load": s.get("training_load"), "aerobic_te": s.get("aerobic_te"), "anaerobic_te": s.get("anaerobic_te"),
        "training_focus": s.get("training_focus"), "perceived_effort": s.get("perceived_effort"),
        "athlete_note": s.get("note"), "calories": s.get("calories"), "water_temp_c": s.get("water_temp_c"),
    })
    return out


# ---------------------------------------------------------------- health views

def health_trends(days: int = 28) -> dict[str, Any]:
    end = date.today()
    recs = health.rows(end - timedelta(days=days - 1), end)
    coros = settings.provider() == "coros"
    keys = ("hrv", "rhr", "sleep_h", "sleep_score", "stress_avg",
            *(("recovery_pct",) if coros else ("bb_wake", "bb_high", "weight_kg")))
    daily = [_clean({k: r[k] for k in ("date", *keys, "hrv_status", "hrv_baseline_low", "hrv_baseline_high",
                                       "cycle_phase")}) for r in recs]
    weekly = []
    for i in range(0, len(recs), 7):
        chunk = recs[i:i + 7]
        row: dict[str, Any] = {"from": chunk[0]["date"]}
        for k in keys:
            vals = [r[k] for r in chunk if isinstance(r[k], (int, float))]
            row[k] = rnd(sum(vals) / len(vals), 1) if vals else None
        weekly.append(_clean(row))
    out = {"daily": [d for d in daily if len(d) > 1], "weekly_avg": weekly, "weight_kg": health.latest_weight()}
    if coros:
        out |= {"coros_fitness": meta_json("coros_fitness"),
                "note": "recovery_pct is COROS's snapshot at the first sync of each day."}
        cycle = meta_json("coros_cycle")
        if cycle.get("cycles") or cycle.get("today_status"):
            out["menstrual_cycle"] = cycle
    return out


def readiness(day: date | None = None) -> dict[str, Any]:
    day = day or date.today()
    result = health.readiness(day, load_today(day))
    yesterday = (day - timedelta(days=1)).isoformat()
    result["yesterday_activities"] = [overview(a) for a in store.activities(since=yesterday, until=yesterday)]
    result["today_activities"] = [overview(a) for a in store.activities(since=day.isoformat(), until=day.isoformat())]
    result["planned_today"] = plan.sessions_on(day) or "nothing planned"
    result["planned_next_3_days"] = plan.sessions(day + timedelta(days=1), day + timedelta(days=3))
    return result


def week_summary(weeks_ago: int = 0) -> dict[str, Any]:
    today = date.today()
    start = today - timedelta(days=today.weekday() + 7 * weeks_ago)
    end = min(start + timedelta(days=6), today)
    acts = store.activities(since=start.isoformat(), until=end.isoformat())
    training = [a for a in acts if a["sport"] not in NON_TRAINING_SPORTS]
    hrecs = health.rows(start, end)

    def avg(key: str) -> float | None:
        vals = [r[key] for r in hrecs if isinstance(r[key], (int, float))]
        return rnd(sum(vals) / len(vals), 1) if vals else None

    return {
        "week": f"{start}..{end}",
        "activities": [overview(a) for a in acts],
        "totals": {"tss": rnd(sum(num(a["metrics"].get("tss")) or 0 for a in training)),
                   "hours": rnd(sum(_hours(a) for a in training), 1)},
        "by_sport": _sport_totals(training),
        "zones": zone_distribution(days=(end - start).days + 1, end=end) if training else None,
        "health_avg": _clean({k: avg(k) for k in ("hrv", "rhr", "sleep_h", "sleep_score", "stress_avg", "recovery_pct",
                                                    "bb_wake")}),
        "load_end_of_week": load_today(end),
        "plan_vs_actual": plan.compliance(start, end),
        "blocks": plan.blocks(start, start + timedelta(days=13)),
        "next_week_planned": plan.sessions(start + timedelta(days=7), start + timedelta(days=13)),
    }
