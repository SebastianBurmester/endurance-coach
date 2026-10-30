"""Longer-range views for the dashboard tabs and the coach: progress, recovery, one sport, one session, races.

Each function returns compact JSON-able dicts. Trends compare the last 6 weeks with the 6 before; "better"
says which direction is an improvement for that measure.
"""

from __future__ import annotations

import statistics
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from typing import Any

import numpy as np

from . import athlete, coach, health, metrics, plan, settings, store
from .athlete import s_to_pace
from .config import MMP_DURATIONS, NON_TRAINING_SPORTS
from .util import date_range, num, rnd, to_date

ENDURANCE = ("swim", "bike", "run")
_TREND_DAYS = 42
_MAX_STREAM_POINTS = 700
# Durations shown as monthly bests on the progress page.
_BEST_DURATIONS = (60, 300, 1200)


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != {} and v != []}


def _hours(a: dict[str, Any]) -> float:
    return (num(a["summary"].get("duration_s")) or num(a["metrics"].get("recorded_s")) or 0) / 3600


# Faster than this (km/h) is a unit glitch in the source (seen: an indoor run in centimetres), not a real distance.
_MAX_KMH = {"run": 30, "swim": 8, "walk": 15, "bike": 80}


def _km(a: dict[str, Any]) -> float:
    km = (num(a["summary"].get("distance_m")) or 0) / 1000
    hours = _hours(a)
    return 0.0 if hours and km / hours > _MAX_KMH.get(a["sport"], 100) else km


_FLAG_KINDS = (("HRV 7-day", "HRV 7-day avg below range"), ("Last-night HRV", "Low HRV night"),
               ("COROS HRV", "COROS HRV below normal"), ("Garmin HRV", "Garmin HRV status"),
               ("Resting HR", "Resting HR up"), ("Short sleep", "Short sleep"), ("Poor sleep", "Poor sleep score"),
               ("7-day sleep", "Sleep debt"), ("TSB", "Deep fatigue (TSB)"), ("CTL ramp", "Fast CTL ramp"),
               ("COROS recovery", "Low COROS recovery"), ("Body Battery", "Low Body Battery"))


def _flag_kind(flag: str) -> str:
    return next((kind for prefix, kind in _FLAG_KINDS if flag.startswith(prefix)), flag.split(" (")[0])


def _tss(a: dict[str, Any]) -> float:
    return num(a["metrics"].get("tss")) or 0.0


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _indoor(a: dict[str, Any]) -> bool:
    t = (a.get("type") or "").lower()
    return any(k in t for k in ("indoor", "virtual", "trainer", "treadmill"))


def _open_water(a: dict[str, Any]) -> bool:
    return "open" in (a.get("type") or "").lower()


def _trend(points: list[dict[str, Any]], better: int, end: date | None = None) -> dict[str, Any]:
    """Points {date, value, ...} with a trailing 28-day median, and the last 6 weeks vs the 6 before."""
    end = end or date.today()
    points = sorted(points, key=lambda p: p["date"])
    for p in points:
        lo = (to_date(p["date"]) - timedelta(days=27)).isoformat()
        p["avg"] = rnd(statistics.median(q["value"] for q in points if lo <= q["date"] <= p["date"]), 3)
    cut, prev_cut = (end - timedelta(days=_TREND_DAYS)).isoformat(), (end - timedelta(days=2 * _TREND_DAYS)).isoformat()
    recent = [p["value"] for p in points if p["date"] > cut]
    previous = [p["value"] for p in points if prev_cut < p["date"] <= cut]
    out: dict[str, Any] = {"points": points, "better": better, "n": len(points)}
    if len(recent) >= 3 and len(previous) >= 3:
        r, q = statistics.median(recent), statistics.median(previous)
        out |= {"recent": rnd(r, 3), "previous": rnd(q, 3), "change_pct": rnd((r - q) / q * 100, 1) if q else None}
        change = out["change_pct"]
        if change is not None:
            # better=0: a measure with no right direction (bike cadence) only goes up or down.
            out["verdict"] = ("stable" if abs(change) < 2 else ("up" if change > 0 else "down") if not better else
                              "improving" if change * better > 0 else "declining")
    return out


def _point(a: dict[str, Any], value: float, **extra: Any) -> dict[str, Any]:
    return _clean({"date": a["date"], "value": value, "id": a["id"], "name": a["name"], **extra})


# ---------------------------------------------------------------- shared sport pieces

def _power_curve(end: date) -> dict[str, Any]:
    """Best power per duration: last 90 days vs the last 365, with FTP / CP estimates from the 90 days."""
    cur = coach._best_efforts(end - timedelta(days=89), end)
    year = coach._best_efforts(end - timedelta(days=364), end)
    th = coach.thresholds(end)
    rows = []
    for d in MMP_DURATIONS:
        c, y = cur.get(str(d)), year.get(str(d))
        if not c and not y:
            continue
        rows.append(_clean({"s": d, "label": coach._label(d), "w_90d": rnd(c["w"]) if c else None,
                            "date_90d": c["date"] if c else None, "id_90d": c["activity_id"] if c else None,
                            "w_365d": rnd(y["w"]) if y else None, "date_365d": y["date"] if y else None,
                            "wkg_90d": round(c["w"] / th["weight_kg"], 2) if c and th["weight_kg"] else None}))
    est = metrics.ftp_estimates({k: v["w"] for k, v in cur.items()})
    return {"rows": rows, "estimates": est, "ftp": th["ftp"], "ftp_source": th["ftp_source"], "weight_kg": th["weight_kg"]}


def _zones(days: int, sport: str, end: date) -> dict[str, Any]:
    seconds: dict[str, float] = defaultdict(float)
    power: dict[str, float] = defaultdict(float)
    basis, with_zones, without = None, 0, 0
    for a in store.activities(since=(end - timedelta(days=days - 1)).isoformat(), until=end.isoformat(), sport=sport):
        with_zones += bool(a["metrics"].get("hr_zones_s"))
        without += not a["metrics"].get("hr_zones_s")
        for k, v in (a["metrics"].get("hr_zones_s") or {}).items():
            seconds[k] += v
        for k, v in (a["metrics"].get("power_zones_s") or {}).items():
            power[k] += v
        basis = a["metrics"].get("hr_zone_basis") or basis

    def table(zs: dict[str, float]) -> list[dict[str, Any]]:
        total = sum(zs.values()) or 1
        return [{"zone": k, "h": rnd(v / 3600, 2), "pct": rnd(100 * v / total, 1)} for k, v in zs.items()]

    out: dict[str, Any] = {"days": days, "hr": table(seconds) if seconds else [], "power": table(power) if power else [],
                           "hr_basis": basis, "sessions_with_zones": with_zones, "sessions_without": without}
    if seconds:
        names = list(seconds)
        total = sum(seconds.values()) or 1
        out["three_zone"] = {"low_pct": rnd(100 * sum(seconds[n] for n in names[:2]) / total, 1),
                             "mid_pct": rnd(100 * sum(seconds[n] for n in names[2:4]) / total, 1),
                             "high_pct": rnd(100 * sum(seconds[n] for n in names[4:]) / total, 1)}
    return out


def _period_totals(acts: list[dict[str, Any]]) -> dict[str, Any]:
    return _clean({"sessions": len(acts), "hours": rnd(sum(_hours(a) for a in acts), 1),
                   "km": rnd(sum(_km(a) for a in acts), 1) or None, "tss": rnd(sum(_tss(a) for a in acts)),
                   "elevation_m": rnd(sum(num(a["summary"].get("elevation_gain_m")) or 0 for a in acts)) or None,
                   "longest_h": rnd(max((_hours(a) for a in acts), default=0), 1) or None,
                   "longest_km": rnd(max((_km(a) for a in acts), default=0), 1) or None})


# ---------------------------------------------------------------- progress

def progress(end: date | None = None) -> dict[str, Any]:
    """Is the athlete getting fitter: thresholds, fitness (CTL), monthly volume, and per-sport efficiency trends."""
    end = end or date.today()
    start = end - timedelta(days=364)
    acts = store.activities(since=start.isoformat(), until=end.isoformat())
    training = [a for a in acts if a["sport"] not in NON_TRAINING_SPORTS]
    th = coach.thresholds(end)
    profile = athlete.load()

    def history(key: str, pace: bool) -> list[dict[str, Any]]:
        return [_clean({"date": e["date"], "value": e["value"], "pace": s_to_pace(e["value"]) if pace else None,
                        "source": e.get("source")}) for e in profile.get(key) or []]

    pmc = coach.pmc_series(start, end)
    six_weeks = next((r for r in pmc if r["date"] == (end - timedelta(days=42)).isoformat()), None)
    peak = max(pmc, key=lambda r: r["ctl"] or 0)
    fitness = {"series": [r for i, r in enumerate(pmc) if i % 3 == 0 or i == len(pmc) - 1],
               "ctl": pmc[-1]["ctl"], "ctl_6w_ago": six_weeks["ctl"] if six_weeks else None,
               "peak": {"date": peak["date"], "ctl": peak["ctl"]}}

    months: dict[str, dict[str, float]] = {}
    m = date(start.year, start.month, 1)
    while m <= end:
        months[m.isoformat()[:7]] = defaultdict(float)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    for a in training:
        bucket = months.get(a["date"][:7])
        if bucket is not None:
            sport = a["sport"] if a["sport"] in (*ENDURANCE, "strength") else "other"
            bucket[sport] += _hours(a)
            bucket["tss"] += _tss(a)
    monthly = [{"month": k, **{s: rnd(v.get(s, 0), 1) for s in (*ENDURANCE, "strength", "other")},
                "tss": rnd(v.get("tss", 0))} for k, v in months.items()]

    # Bike: monthly bests and endurance-ride efficiency (NP per heartbeat).
    bests: dict[int, list[dict[str, Any]]] = {d: [] for d in _BEST_DURATIONS}
    for month in months:
        mstart = date.fromisoformat(month + "-01")
        mend = min(date(mstart.year + (mstart.month == 12), mstart.month % 12 + 1, 1) - timedelta(days=1), end)
        b = coach._best_efforts(mstart, mend)
        for d in _BEST_DURATIONS:
            if b.get(str(d)):
                bests[d].append({"date": month + "-15", "value": rnd(b[str(d)]["w"]), "id": b[str(d)]["activity_id"]})
    bike = [a for a in training if a["sport"] == "bike"]
    ride_ef = [_point(a, a["metrics"]["ef"], np=a["metrics"].get("np"), hr=a["metrics"].get("avg_hr"))
               for a in bike if a["metrics"].get("ef") and a["metrics"].get("np") and _hours(a) >= 0.75
               and (a["metrics"].get("if") or 0) < 0.8]

    runs = [a for a in training if a["sport"] == "run"]
    easy_runs = [a for a in runs if a["metrics"].get("ef") and _hours(a) >= 0.4 and (a["metrics"].get("if") or 0) < 0.85]
    run_ef = [_point(a, a["metrics"]["ef"], pace=s_to_pace(a["metrics"].get("ngp_s_km")), hr=a["metrics"].get("avg_hr"))
              for a in easy_runs]
    long_dec = [_point(a, a["metrics"]["decoupling"]["decoupling_pct"])
                for a in runs if _hours(a) >= 1 and (a["metrics"].get("decoupling") or {}).get("decoupling_pct") is not None]
    cadence = [_point(a, a["metrics"]["avg_cadence"]) for a in runs if (a["metrics"].get("avg_cadence") or 0) > 120]
    vo2 = [{"date": a["date"], "value": a["summary"]["vo2max"]} for a in runs if a["summary"].get("vo2max")]
    if settings.provider() == "coros":
        snaps = store.daily(start.isoformat(), end.isoformat())
        vo2 = [{"date": d, "value": src["coros_fitness"]["vo2max"]} for d, src in snaps.items()
               if (src.get("coros_fitness") or {}).get("vo2max")]
        current = coach.meta_json("coros_fitness")
        if current.get("vo2max") and not any(p["date"] == end.isoformat() for p in vo2):
            vo2.append({"date": end.isoformat(), "value": current["vo2max"]})

    swims = [a for a in training if a["sport"] == "swim" and not _open_water(a)]
    swim_pace = [_point(a, a["metrics"]["avg_pace_s_100m"], pace=s_to_pace(a["metrics"]["avg_pace_s_100m"]), km=rnd(_km(a), 2))
                 for a in swims if a["metrics"].get("avg_pace_s_100m") and _km(a) >= 0.4]

    out: dict[str, Any] = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "thresholds": {
            "ftp": {"current": th["ftp"], "source": th["ftp_source"], "history": history("ftp_history", False),
                    "wkg": round(th["ftp"] / th["weight_kg"], 2) if th["ftp"] and th["weight_kg"] else None},
            "run": {"current_s_km": th["run_threshold_s_km"], "current": th["run_threshold_pace"],
                    "source": th["run_threshold_source"], "history": history("run_threshold_history", True)},
            "css": {"current_s_100m": th["css_s_100m"], "current": th["css_pace"], "source": th["css_source"],
                    "history": history("css_history", True)},
            "lthr_bike": th["lthr_bike"], "lthr_run": th["lthr_run"], "lthr_est": th["lthr_est"],
            "max_hr": th["max_hr"], "weight_kg": th["weight_kg"],
        },
        "fitness": fitness,
        "monthly": monthly,
        "bike": {"monthly_bests": {coach._label(d): v for d, v in bests.items() if v},
                 "endurance_ef": _trend(ride_ef, 1, end), "power_curve": _power_curve(end) if bike else None},
        "run": {"easy_ef": _trend(run_ef, 1, end), "long_decoupling": _trend(long_dec, -1, end),
                "cadence": _trend(cadence, 1, end), "vo2max": _trend(vo2, 1, end) if vo2 else None,
                "predictions": coach.meta_json("coros_fitness").get("race_predictions")},
        "swim": {"pace": _trend(swim_pace, -1, end)},
    }
    out["highlights"] = _progress_highlights(out)
    return out


def _progress_highlights(p: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(level: str, text: str) -> None:
        out.append({"level": level, "text": text})

    f = p["fitness"]
    if f["ctl"] is not None and f["ctl_6w_ago"] is not None:
        d = f["ctl"] - f["ctl_6w_ago"]
        add("good" if d > 2 else "info" if d > -2 else "warn",
            f"Fitness (CTL) {f['ctl']:.0f}, {'+' if d >= 0 else ''}{d:.0f} in 6 weeks (peak this year {f['peak']['ctl']:.0f} on {f['peak']['date']}).")
    for label, t, unit in (("Easy-run efficiency", p["run"]["easy_ef"], ""), ("Endurance-ride efficiency (NP/HR)", p["bike"]["endurance_ef"], ""),
                           ("Pool pace", p["swim"]["pace"], ""), ("HR drift on long runs", p["run"]["long_decoupling"], ""),
                           ("Run cadence", p["run"]["cadence"], "")):
        if t and t.get("verdict"):
            word = {"improving": "improving", "declining": "slipping", "stable": "stable"}[t["verdict"]]
            add({"improving": "good", "declining": "warn", "stable": "info"}[t["verdict"]],
                f"{label} {word}: {t['change_pct']:+.1f}% vs the 6 weeks before ({t['n']} sessions in the year).")
    pc = p["bike"].get("power_curve")
    if pc and pc["estimates"]:
        est = pc["estimates"]
        guess = (est.get("critical_power") or {}).get("cp_w") or est.get("ftp_from_20min")
        if guess and pc["ftp"]:
            diff = (guess - pc["ftp"]) / pc["ftp"] * 100
            if abs(diff) >= 4:
                add("info", f"Recent bests point to an FTP near {guess:.0f} W ({diff:+.0f}% vs the {pc['ftp']:.0f} W in use). "
                            "Worth a test before changing it.")
    vo2 = p["run"].get("vo2max")
    if vo2 and vo2.get("points") and len(vo2["points"]) >= 2:
        first, last = vo2["points"][0]["value"], vo2["points"][-1]["value"]
        if first != last:
            add("good" if last > first else "info", f"VO2max {first:g} → {last:g} over the window.")
    return out


# ---------------------------------------------------------------- recovery

def recovery(days: int = 120, end: date | None = None) -> dict[str, Any]:
    """Readiness day by day, HRV / RHR / sleep against load, how quickly big days are absorbed, strain episodes."""
    end = end or date.today()
    start = end - timedelta(days=days - 1)
    hist = health.rows(start - timedelta(days=60), end)
    pmc = {r["date"]: r for r in coach.pmc_series(start - timedelta(days=7), end)}
    daily_tss = defaultdict(float)
    for a in store.activities(since=(start - timedelta(days=40)).isoformat(), until=end.isoformat()):
        if a["sport"] not in NON_TRAINING_SPORTS:
            daily_tss[a["date"]] += _tss(a)

    out_days = []
    for i in range(60, len(hist)):
        day = to_date(hist[i]["date"])
        r = health.readiness(day, pmc.get(day.isoformat()), history=hist[i - 60:i + 1])
        rec = hist[i]
        out_days.append(_clean({
            "date": rec["date"], "status": r["status"] if r["data_present"] else None, "flags": r["flags"],
            "hrv": rec["hrv"], "hrv_7d": r["hrv"].get("avg_7d"), "hrv_range": r["hrv"].get("normal_range_60d"),
            "rhr": rec["rhr"], "rhr_delta": r["resting_hr"].get("delta"), "sleep_h": rec["sleep_h"],
            "sleep_score": rec["sleep_score"], "stress": rec["stress_avg"],
            "recovery": rec["recovery_pct"] if settings.provider() == "coros" else (rec["bb_wake"] or rec["bb_high"]),
            "tss": rnd(daily_tss.get(rec["date"], 0)), "tsb": (pmc.get(rec["date"]) or {}).get("tsb"),
            "ctl": (pmc.get(rec["date"]) or {}).get("ctl"),
        }))

    counts = lambda ds: {s: sum(d.get("status") == s for d in ds) for s in ("green", "amber", "red")}  # noqa: E731
    major = ("HRV 7-day", "Last-night HRV", "Resting HR")
    episodes, run = [], []
    for d in out_days + [{}]:
        strained = d.get("status") == "red" or (any(f.startswith(major) for f in d.get("flags") or []) and d.get("status") != "green")
        if strained and d:
            run.append(d)
            continue
        if len(run) >= 2:
            first, last = run[0]["date"], run[-1]["date"]
            before = sum(daily_tss.get((to_date(first) - timedelta(days=k)).isoformat(), 0) for k in range(1, 8))
            kinds: dict[str, int] = defaultdict(int)
            for x in run:
                for f in x.get("flags") or []:
                    kinds[_flag_kind(f)] += 1
            flags = [k for k, _ in sorted(kinds.items(), key=lambda kv: -kv[1])]
            episodes.append({"start": first, "end": last, "days": len(run), "red_days": sum(x.get("status") == "red" for x in run),
                             "flags": flags[:5], "tss_7d_before": rnd(before)})
        run = []

    # Bounce-back: HRV / RHR / sleep score on the mornings after the biggest days, vs the 30 days before each.
    tss_days = [v for k, v in daily_tss.items() if v > 0 and start.isoformat() <= k <= end.isoformat()]
    big_cut = max(80.0, float(np.percentile(tss_days, 85))) if len(tss_days) >= 10 else None
    by_date = {r["date"]: r for r in hist}
    curves: dict[int, dict[str, list[float]]] = {k: defaultdict(list) for k in range(1, 5)}
    big_days = []
    if big_cut:
        for day_s, v in sorted(daily_tss.items()):
            if v < big_cut or not start.isoformat() <= day_s <= (end - timedelta(days=1)).isoformat():
                continue
            day = to_date(day_s)
            base = [by_date[(day - timedelta(days=k)).isoformat()] for k in range(1, 31)
                    if (day - timedelta(days=k)).isoformat() in by_date]
            bh = [b["hrv"] for b in base if b["hrv"]]
            br = [b["rhr"] for b in base if b["rhr"]]
            bs = [b["sleep_score"] for b in base if b["sleep_score"]]
            big_days.append({"date": day_s, "tss": rnd(v)})
            for k in range(1, 5):
                r = by_date.get((day + timedelta(days=k)).isoformat())
                if not r:
                    continue
                if r["hrv"] and len(bh) >= 10:
                    curves[k]["hrv_pct"].append((r["hrv"] - statistics.fmean(bh)) / statistics.fmean(bh) * 100)
                if r["rhr"] and len(br) >= 10:
                    curves[k]["rhr"].append(r["rhr"] - statistics.fmean(br))
                if r["sleep_score"] and len(bs) >= 10:
                    curves[k]["sleep_score"].append(r["sleep_score"] - statistics.fmean(bs))
    bounce = [{"morning": k, **{m: rnd(statistics.fmean(v), 1) for m, v in c.items() if v},
               "n": max((len(v) for v in c.values()), default=0)} for k, c in curves.items()]
    back = next((b["morning"] for b in bounce if b.get("hrv_pct") is not None and b["hrv_pct"] >= -2), None)

    weekly = []
    for w in date_range(_monday(start), end):
        if w.weekday():
            continue
        wd = [d for d in out_days if w.isoformat() <= d["date"] <= (w + timedelta(days=6)).isoformat()]
        if not wd:
            continue
        avg = lambda k: rnd(statistics.fmean([d[k] for d in wd if d.get(k) is not None]), 1) if any(d.get(k) is not None for d in wd) else None  # noqa: E731
        weekly.append(_clean({"week_of": w.isoformat(), "tss": rnd(sum(d.get("tss", 0) for d in wd)), "hrv": avg("hrv"),
                              "rhr": avg("rhr"), "sleep_h": avg("sleep_h"), "ctl": wd[-1].get("ctl"), **counts(wd)}))

    last30 = out_days[-30:]
    summary = []
    c30 = counts(last30)
    if last30:
        summary.append(f"Last 30 days: {c30['green']} green, {c30['amber']} amber, {c30['red']} red mornings.")
    if back:
        summary.append(f"After big days (≥ {big_cut:.0f} TSS) your HRV is back near baseline on morning {back}.")
    elif big_cut and any(b.get("hrv_pct") is not None for b in bounce):
        summary.append(f"After big days (≥ {big_cut:.0f} TSS) HRV stays below baseline for 4+ mornings: space them out.")
    if episodes:
        e = episodes[-1]
        summary.append(f"Latest strain episode: {e['start']} to {e['end']} ({e['days']} days: {', '.join(e['flags'][:3])}).")
    return {"window": {"start": start.isoformat(), "end": end.isoformat()}, "days": out_days, "counts_30d": c30,
            "counts_all": counts(out_days), "episodes": episodes[-12:], "big_day_tss": rnd(big_cut) if big_cut else None,
            "big_days": big_days[-20:], "bounce_back": bounce, "back_to_baseline_morning": back, "weekly": weekly,
            "summary": summary}


# ---------------------------------------------------------------- one sport

def sport_view(sport: str, end: date | None = None) -> dict[str, Any]:
    """Volume, ramp, zones, sport-specific trends and the session list for swim, bike, run or strength."""
    end = end or date.today()
    start = end - timedelta(days=364)
    acts = store.activities(since=start.isoformat(), until=end.isoformat(), sport=sport)
    since = lambda d: (end - timedelta(days=d - 1)).isoformat()  # noqa: E731
    periods = {k: _period_totals([a for a in acts if a["date"] >= since(d)]) for k, d in (("7d", 7), ("28d", 28), ("84d", 84), ("365d", 365))}

    weeks = []
    for w in date_range(_monday(end - timedelta(days=7 * 25)), end):
        if w.weekday():
            continue
        wa = [a for a in acts if w.isoformat() <= a["date"] <= (w + timedelta(days=6)).isoformat()]
        weeks.append({"week_of": w.isoformat(), **_period_totals(wa), "current": w == _monday(end)})
    flags: list[str] = []
    if sport == "run":
        # +10% per week at most, long run at most 30% of the week (complete weeks only).
        for prev, cur in zip(weeks, weeks[1:]):
            if cur["current"]:
                continue
            pk, ck = prev.get("km") or 0, cur.get("km") or 0
            if pk >= 10 and ck > pk * 1.1:
                cur["ramp_pct"] = rnd((ck - pk) / pk * 100)
            if ck >= 15 and cur.get("longest_km") and cur["longest_km"] / ck > 0.3:
                cur["long_share_pct"] = rnd(cur["longest_km"] / ck * 100)
        recent = [w for w in weeks[-5:-1]]
        ramps = [w for w in recent if w.get("ramp_pct")]
        if ramps:
            flags.append(f"Run volume rose more than 10% in {len(ramps)} of the last 4 weeks "
                         f"(up to +{max(w['ramp_pct'] for w in ramps):.0f}%): keep the build to ~10% per week.")
        longs = [w for w in recent if w.get("long_share_pct")]
        if longs:
            flags.append(f"The long run was over 30% of the week in {len(longs)} of the last 4 weeks: add easy volume "
                         "around it rather than making it longer.")

    trends: dict[str, Any] = {}
    if sport == "bike":
        outdoor = [a for a in acts if not _indoor(a) and _hours(a) >= 0.5]
        trends["speed_outdoor"] = _trend([_point(a, num(a["summary"].get("moving_avg_speed_kmh") or a["summary"].get("avg_speed_kmh")))
                                          for a in outdoor if a["summary"].get("moving_avg_speed_kmh") or a["summary"].get("avg_speed_kmh")], 1, end)
        trends["ef"] = _trend([_point(a, a["metrics"]["ef"]) for a in acts if a["metrics"].get("ef") and (a["metrics"].get("if") or 0) < 0.8], 1, end)
        trends["cadence"] = _trend([_point(a, a["metrics"]["avg_cadence"]) for a in acts if a["metrics"].get("avg_cadence")], 0, end)
    elif sport == "run":
        trends["pace"] = _trend([_point(a, a["metrics"]["avg_pace_s_km"], pace=s_to_pace(a["metrics"]["avg_pace_s_km"]))
                                 for a in acts if a["metrics"].get("avg_pace_s_km") and _km(a) >= 2], -1, end)
        trends["ef"] = _trend([_point(a, a["metrics"]["ef"]) for a in acts if a["metrics"].get("ef") and (a["metrics"].get("if") or 0) < 0.85], 1, end)
        trends["cadence"] = _trend([_point(a, a["metrics"]["avg_cadence"]) for a in acts if (a["metrics"].get("avg_cadence") or 0) > 120], 1, end)
        trends["long_run_km"] = _trend([{"date": w["week_of"], "value": w["longest_km"]} for w in weeks if w.get("longest_km")], 1, end)
    elif sport == "swim":
        trends["pace"] = _trend([_point(a, a["metrics"]["avg_pace_s_100m"], pace=s_to_pace(a["metrics"]["avg_pace_s_100m"]))
                                 for a in acts if a["metrics"].get("avg_pace_s_100m") and not _open_water(a) and _km(a) >= 0.4], -1, end)
        trends["distance_km"] = _trend([_point(a, rnd(_km(a), 2)) for a in acts if _km(a)], 1, end)

    split = None
    if sport in ("bike", "run"):
        inside = [a for a in acts if a["date"] >= since(84) and _indoor(a)]
        split = {"indoor": _period_totals(inside), "outdoor": _period_totals([a for a in acts if a["date"] >= since(84) and not _indoor(a)])}
    elif sport == "swim":
        ow = [a for a in acts if a["date"] >= since(84) and _open_water(a)]
        split = {"open_water": _period_totals(ow), "pool": _period_totals([a for a in acts if a["date"] >= since(84) and not _open_water(a)])}

    out: dict[str, Any] = {
        "sport": sport, "periods": periods, "weeks": weeks, "flags": flags, "trends": trends, "split": split,
        "zones_28d": _zones(28, sport, end), "zones_84d": _zones(84, sport, end),
        "sessions": [coach.overview(a) for a in reversed(acts) if a["date"] >= since(84)],
        "available": [s for s in (*ENDURANCE, "strength", "multisport") if store.activities(since=start.isoformat(), sport=s)],
    }
    th = coach.thresholds(end)
    if sport == "bike":
        out["power_curve"] = _power_curve(end)
        out["thresholds"] = {"ftp": th["ftp"], "weight_kg": th["weight_kg"]}
    if sport == "run":
        out["thresholds"] = {"run_threshold": th["run_threshold_pace"], "lthr_run": th["lthr_run"]}
        out["predictions"] = coach.meta_json("coros_fitness").get("race_predictions")
    if sport == "swim":
        out["thresholds"] = {"css": th["css_pace"]}
    z = out["zones_28d"].get("three_zone")
    if z and sport in ENDURANCE and z["mid_pct"] > 25:
        flags.append(f"{z['mid_pct']:.0f}% of the last 4 weeks' {sport} time sat in the moderate zones (Z3-Z4): "
                     "aim for ~80% easy and keep hard sessions hard.")
    return out


# ---------------------------------------------------------------- one session

def _downsample(arr: np.ndarray, step: int, smooth: int = 1) -> list[float | None]:
    if smooth > 1:
        kernel = np.ones(smooth) / smooth
        filled = np.where(np.isnan(arr), np.nanmean(arr) if np.any(~np.isnan(arr)) else 0, arr)
        arr = np.where(np.isnan(arr), np.nan, np.convolve(filled, kernel, mode="same"))
    out = []
    for i in range(0, len(arr), step):
        chunk = arr[i:i + step]
        v = np.nanmean(chunk) if np.any(~np.isnan(chunk)) else None
        out.append(None if v is None else round(float(v), 1))
    return out


_SESSION_CACHE: OrderedDict[tuple[int, str | None], dict[str, Any]] = OrderedDict()
_SESSION_CACHE_SIZE = 12


def session_detail(activity_id: int) -> dict[str, Any]:
    """analyze_session plus stream series for charts, numeric zones and neighbours for paging.

    A long ride's FIT takes seconds to parse on the Pi: it is parsed once per request, and the result is kept for
    the last few sessions until a sync updates the activity.
    """
    a = store.activity(activity_id)
    if not a:
        return {"error": "Activity not found in the cache."}
    key = (activity_id, a.get("updated_at"))
    if key in _SESSION_CACHE:
        _SESSION_CACHE.move_to_end(key)
        return {**_SESSION_CACHE[key], "coach_review": store.session_review(activity_id)}
    stream = coach.load_stream(activity_id) if a["fit_status"] == "ok" else None
    out = coach.analyze_session(activity_id, remote_laps=False, stream=stream)
    m = a["metrics"]
    out["zones_s"] = _clean({"hr": m.get("hr_zones_s"), "power": m.get("power_zones_s"), "hr_basis": m.get("hr_zone_basis")})
    if stream is not None and stream.seconds > 0:
        step = max(1, int(np.ceil(stream.seconds / _MAX_STREAM_POINTS)))
        series: dict[str, Any] = {"step_s": step, "t_min": [round(i / 60, 2) for i in range(0, stream.seconds, step)]}
        if stream.coverage("hr") > 0.3:
            series["hr"] = _downsample(stream.hr, step)
        if a["sport"] == "bike" and stream.coverage("power") > 0.3:
            series["power"] = _downsample(np.nan_to_num(stream.power, nan=0.0), step, smooth=10)
        if stream.coverage("speed") > 0.3:
            speed = _downsample(stream.speed, step, smooth=10)
            if a["sport"] in ("run",):
                series["pace_s_km"] = [round(1000 / v) if v and v > 1.2 else None for v in speed]
            elif a["sport"] == "swim":
                series["pace_s_100m"] = [round(100 / v) if v and v > 0.3 else None for v in speed]
            else:
                series["kmh"] = [round(v * 3.6, 1) if v is not None else None for v in speed]
        if stream.coverage("altitude") > 0.3 and a["sport"] != "swim":
            series["altitude"] = _downsample(stream.altitude, step)
        if stream.coverage("cadence") > 0.3:
            cad = stream.cadence * (2 if a["sport"] == "run" and np.nanmean(stream.cadence) < 120 else 1)
            series["cadence"] = _downsample(np.where(cad > 0, cad, np.nan), step)
        if stream.coverage("temperature") > 0.3:
            series["temperature"] = _downsample(stream.temperature, step)
        out["series"] = series
    same_day = [x for x in store.activities(since=a["date"], until=a["date"])]
    with store.connect() as conn:
        prev = conn.execute("SELECT id FROM activities WHERE start_local < ? AND sport NOT IN ('walk') ORDER BY start_local DESC LIMIT 1",
                            (a["start_local"],)).fetchone()
        nxt = conn.execute("SELECT id FROM activities WHERE start_local > ? AND sport NOT IN ('walk') ORDER BY start_local LIMIT 1",
                           (a["start_local"],)).fetchone()
    out["neighbours"] = _clean({"prev": prev["id"] if prev else None, "next": nxt["id"] if nxt else None,
                                "same_day": [{"id": x["id"], "sport": x["sport"], "name": x["name"]} for x in same_day if x["id"] != a["id"]]})
    out["date"], out["sport"], out["fit_status"] = a["date"], a["sport"], a["fit_status"]
    _SESSION_CACHE[key] = out
    while len(_SESSION_CACHE) > _SESSION_CACHE_SIZE:
        _SESSION_CACHE.popitem(last=False)
    return {**out, "coach_review": store.session_review(activity_id)}


# ---------------------------------------------------------------- races

def races(end: date | None = None) -> dict[str, Any]:
    """Every event in the profile: countdown, projected form on the day, taper, remaining key sessions, race plan;
    past events with what was recorded that day."""
    today = end or date.today()
    profile = athlete.load()
    events = sorted([e for e in profile.get("events") or [] if e.get("date")], key=lambda e: e["date"])
    upcoming = [e for e in events if e["date"] >= today.isoformat()]
    last = max([to_date(e["date"]) for e in upcoming] + [today])
    series = plan.projected_series(today - timedelta(days=60), last + timedelta(days=3))
    by_date = {r["date"]: r for r in series}
    predictions = coach.meta_json("coros_fitness").get("race_predictions")
    out_events = []
    for e in events:
        day = to_date(e["date"])
        rec: dict[str, Any] = {k: v for k, v in e.items()}
        rec["days_to_go"] = (day - today).days
        if day >= today:
            proj = by_date.get(e["date"]) or {}
            rec["projected"] = _clean({k: proj.get(k) for k in ("ctl", "atl", "tsb")})
            tsb = proj.get("tsb")
            # A races get a full taper, B races a short one; C races are trained through.
            prio = str(e.get("priority") or "B").upper()
            lo, hi = {"A": (10, 25), "B": (5, 20)}.get(prio, (-10, 25))
            rec["form_target"] = [lo, hi]
            if tsb is not None:
                if prio == "C":
                    rec["form_verdict"] = ("fine for a C race (trained through)" if lo <= tsb <= hi else
                                           "very fresh for a C race" if tsb > hi else "still fatigued, even for a C race")
                else:
                    rec["form_verdict"] = (f"fresh, as targeted (+{lo} to +{hi})" if lo <= tsb <= hi else
                                           "very fresh: may give away fitness" if tsb > hi else
                                           "a bit flat: taper deeper" if 0 <= tsb < lo else "still fatigued: plan a taper")
            ahead = plan.sessions(today, day)
            active = [s for s in ahead if s.get("status") != "skipped" and s.get("type") != "rest"]
            rec["plan"] = _clean({
                "sessions": len(active), "hours": rnd(sum(s.get("duration_min") or 0 for s in active) / 60, 1),
                "tss": rnd(sum(plan.estimated_tss(s) or 0 for s in active)),
                "covered_until": max((s["date"] for s in ahead), default=None),
                "key_sessions": [_clean({"date": s["date"], "sport": s.get("sport"), "title": s["title"], "type": s.get("type")})
                                 for s in active if plan.INTENSITY_CLASS.get(s.get("type")) == "hard"
                                 and s["date"] >= (day - timedelta(days=42)).isoformat()][-8:],
            })
            taper = [b for b in plan.blocks(day - timedelta(days=21), day) if "taper" in f"{b.get('phase')} {b.get('name')}".lower()]
            last10 = sum(plan.estimated_tss(s) or 0 for s in active if s["date"] > (day - timedelta(days=10)).isoformat())
            prior10 = sum(plan.estimated_tss(s) or 0 for s in active
                          if (day - timedelta(days=20)).isoformat() < s["date"] <= (day - timedelta(days=10)).isoformat())
            rec["taper"] = _clean({"block": taper[0]["name"] if taper else None,
                                   "last_10d_tss": rnd(last10), "prior_10d_tss": rnd(prior10),
                                   "reduction_pct": rnd((1 - last10 / prior10) * 100) if prior10 else None})
            rec["blocks"] = [{"id": b["id"], "name": b["name"], "phase": b.get("phase"), "start_date": b["start_date"],
                              "end_date": b["end_date"]} for b in plan.blocks(today, day)]
        else:
            rec["activities"] = [coach.overview(a) for a in store.activities(since=e["date"], until=e["date"])
                                 if a["sport"] not in NON_TRAINING_SPORTS]
            pre = by_date.get(e["date"])
            if pre:
                rec["form_on_the_day"] = _clean({k: pre.get(k) for k in ("ctl", "atl", "tsb")})
        out_events.append(_clean(rec))
    for r in series:
        r["projected"] = r["date"] > today.isoformat()
    return {"today": today.isoformat(), "events": out_events, "predictions": predictions,
            "form": series, "ctl_today": (by_date.get(today.isoformat()) or {}).get("ctl")}


def save_race_plan(event_date: str, race_plan: str | None = None, result: str | None = None,
                   name: str | None = None, course: dict[str, float | None] | None = None) -> dict[str, Any]:
    """Store the coach's race plan (pacing, fueling, logistics), the result or the course (distance_km, elevation_m,
    expected_duration_h) for one event in athlete.json."""
    profile = athlete.load()
    matches = [e for e in profile.get("events") or [] if e.get("date") == event_date and (not name or e.get("name") == name)]
    if not matches:
        return {"error": f"No event on {event_date}" + (f" named {name!r}" if name else "") + ". Add it with update_athlete_profile first."}
    event = matches[0]
    stamp = date.today().isoformat()
    if race_plan is not None:
        event["race_plan"], event["race_plan_updated"] = race_plan.strip(), stamp
    if result is not None:
        event["result"], event["result_updated"] = result.strip(), stamp
    event.update({k: v for k, v in (course or {}).items() if v is not None})
    athlete.save(profile)
    return {"saved": event}
