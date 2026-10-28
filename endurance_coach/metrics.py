"""Training metrics for bike, run and swim: NP/IF/TSS, NGP/rTSS, sTSS, hrTSS, zones, decoupling, PMC."""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Any

import numpy as np

from .config import HR_ZONES_BIKE, HR_ZONES_MAX, HR_ZONES_RUN, MMP_DURATIONS, POWER_ZONES
from .fitfile import Stream
from .util import num, rnd

_MIN_POWER_COVERAGE = 0.8
_MIN_SPEED_COVERAGE = 0.7


def rolling_mean(x: np.ndarray, window: int) -> np.ndarray:
    if len(x) < window:
        return np.array([])
    c = np.concatenate(([0.0], np.cumsum(x)))
    return (c[window:] - c[:-window]) / window


def normalized_power(power: np.ndarray) -> float | None:
    r = rolling_mean(power, 30)
    if not len(r):
        return None
    return float(np.mean(r**4) ** 0.25)


def mean_max(power: np.ndarray, durations: list[int] = MMP_DURATIONS) -> dict[str, float]:
    out = {}
    for d in durations:
        r = rolling_mean(power, d)
        if len(r):
            out[str(d)] = float(r.max())
    return out


def _zone_times(values: np.ndarray, threshold: float, zones: list[tuple[str, float]]) -> dict[str, int]:
    v = values[~np.isnan(values)]
    out, lower = {}, -math.inf
    for name, upper in zones:
        out[name] = int(np.sum((v > lower * threshold) & (v <= upper * threshold)))
        lower = upper
    return out


def tss_from_np(np_w: float, seconds: float, ftp: float) -> float:
    intensity = np_w / ftp
    return seconds * np_w * intensity / (ftp * 3600) * 100


def hr_tss(seconds: float, avg_hr: float, lthr: float) -> float:
    """TrainingPeaks-style hrTSS approximation: hours * (avgHR/LTHR)^2 * 100."""
    return seconds / 3600 * (avg_hr / lthr) ** 2 * 100


def r_tss(seconds: float, ngp_s_km: float, threshold_s_km: float) -> float:
    """Running TSS: hours * IF^2 * 100 with IF = threshold pace / normalized graded pace."""
    return seconds / 3600 * (threshold_s_km / ngp_s_km) ** 2 * 100


def s_tss(seconds: float, pace_s_100m: float, css_s_100m: float) -> float:
    """Swim TSS: hours * IF^3 * 100 with IF = CSS / pace (water resistance rises with the cube of speed)."""
    return seconds / 3600 * (css_s_100m / pace_s_100m) ** 3 * 100


def lthr_for(sport: str, th: dict[str, Any]) -> float | None:
    """LTHR for the sport: its own, derived from the other sport's (run ~5 bpm higher), else the max-HR estimate."""
    if sport == "run":
        return th.get("lthr_run") or (th["lthr_bike"] + 5 if th.get("lthr_bike") else None) or th.get("lthr_est")
    return th.get("lthr_bike") or (th["lthr_run"] - 5 if th.get("lthr_run") else None) or th.get("lthr_est")


def _lthr_estimated(th: dict[str, Any]) -> bool:
    return not (th.get("lthr_run") or th.get("lthr_bike")) and bool(th.get("lthr_est"))


def _grade_factor(grade: np.ndarray) -> np.ndarray:
    """Minetti et al. (2002) energy cost of running on a grade, relative to flat."""
    g = np.clip(grade, -0.45, 0.45)
    cost = 155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3 + 46.3 * g**2 + 19.5 * g + 3.6
    return np.clip(cost / 3.6, 0.5, 4.0)


def graded_speed(stream: Stream) -> np.ndarray:
    """Grade-adjusted speed (m/s), grade taken over a 10-second window from altitude and distance."""
    speed = np.nan_to_num(stream.speed, nan=0.0)
    if stream.coverage("altitude") < 0.7 or stream.coverage("distance") < 0.7 or len(speed) < 20:
        return speed
    alt = np.nan_to_num(stream.altitude, nan=float(np.nanmean(stream.altitude)))
    dist = np.maximum.accumulate(np.nan_to_num(stream.distance, nan=0.0))
    w = 10
    d_alt = np.concatenate((np.zeros(w), alt[w:] - alt[:-w]))
    d_dist = np.concatenate((np.zeros(w), dist[w:] - dist[:-w]))
    grade = np.where(d_dist > 5, d_alt / np.maximum(d_dist, 1e-6), 0.0)
    return speed * _grade_factor(grade)


def normalized_graded_pace(stream: Stream) -> float | None:
    """NGP in s/km: 4th-power mean of 30 s rolling grade-adjusted speed (moving samples only)."""
    gs = graded_speed(stream)
    moving = gs[gs > 0.5]
    r = rolling_mean(moving, 30)
    if not len(r):
        return None
    speed = float(np.mean(r**4) ** 0.25)
    return 1000 / speed if speed > 0 else None


def decoupling(work: np.ndarray, hr: np.ndarray, skip_s: int = 600) -> dict[str, float] | None:
    """Aerobic decoupling (Friel): output per heartbeat, first vs second half. work = power (bike) or speed (run).
    Positive % = HR drifted up relative to output in the 2nd half."""
    mask = ~np.isnan(work) & ~np.isnan(hr) & (hr > 0) & (work > 0)
    p, h = work[mask], hr[mask]
    if len(p) > 3600:
        p, h = p[skip_s:], h[skip_s:]
    if len(p) < 1200:
        return None
    half = len(p) // 2
    ef1 = float(np.mean(p[:half]) / np.mean(h[:half]))
    ef2 = float(np.mean(p[half:]) / np.mean(h[half:]))
    return {"ef_first_half": round(ef1, 4), "ef_second_half": round(ef2, 4), "decoupling_pct": round((ef1 - ef2) / ef1 * 100, 1)}


def _hr_part(stream: Stream, sport: str, th: dict[str, Any], m: dict[str, Any]) -> None:
    if stream.coverage("hr") <= 0.5:
        return
    hr = stream.hr
    m["avg_hr"] = rnd(np.nanmean(hr))
    m["max_hr"] = rnd(np.nanmax(hr))
    lthr = lthr_for(sport, th)
    if lthr:
        m["hr_zones_s"] = _zone_times(hr, lthr, HR_ZONES_RUN if sport == "run" else HR_ZONES_BIKE)
        m["hr_zone_basis"] = f"{'run' if sport == 'run' else 'bike'} LTHR {lthr:.0f}" + (
            " (estimated from max HR)" if _lthr_estimated(th) else "")
    elif th.get("max_hr"):
        m["hr_zones_s"] = _zone_times(hr, th["max_hr"], HR_ZONES_MAX)
        m["hr_zone_basis"] = f"max HR {th['max_hr']:.0f}"


def stream_metrics(sport: str, stream: Stream, th: dict[str, Any]) -> dict[str, Any]:
    """Metric set for one activity computed from its 1 Hz stream. TSS only when the stream supports it;
    the caller falls back to the summary (watch TSS / load, hrTSS) otherwise."""
    m: dict[str, Any] = {"recorded_s": stream.seconds, "elapsed_s": round(stream.elapsed_s)}
    _hr_part(stream, sport, th, m)
    hr = stream.hr
    if stream.coverage("cadence") > 0.5:
        cad = stream.cadence[~np.isnan(stream.cadence)]
        moving = cad[cad > 0]
        m["avg_cadence"] = rnd(np.mean(moving) * (2 if sport == "run" and np.mean(moving) < 120 else 1)) if len(moving) else None
        if sport == "bike":
            m["coasting_pct"] = rnd(100 * np.mean(cad == 0), 1)
    if stream.coverage("temperature") > 0.5:
        m["avg_temp_c"] = rnd(np.nanmean(stream.temperature), 1)

    if sport == "bike" and stream.coverage("power") >= _MIN_POWER_COVERAGE:
        power = np.nan_to_num(stream.power, nan=0.0)
        np_w = normalized_power(power)
        avg = float(np.mean(power))
        m.update(avg_power=rnd(avg), max_power=rnd(power.max()), np=rnd(np_w), kj=rnd(power.sum() / 1000),
                 vi=rnd(np_w / avg, 2) if np_w and avg else None, mmp=mean_max(power))
        if m.get("avg_hr") and np_w:
            m["ef"] = round(np_w / m["avg_hr"], 2)
        if stream.coverage("hr") > 0.5:
            m["decoupling"] = decoupling(stream.power, hr)
        if th.get("weight_kg") and np_w:
            m["np_wkg"] = round(np_w / th["weight_kg"], 2)
        if th.get("ftp") and np_w:
            m["ftp_used"] = th["ftp"]
            m["if"] = round(np_w / th["ftp"], 2)
            m["tss"] = rnd(tss_from_np(np_w, stream.seconds, th["ftp"]))
            m["tss_source"] = "power"
            m["power_zones_s"] = _zone_times(power, th["ftp"], POWER_ZONES)

    if sport == "run" and stream.coverage("speed") >= _MIN_SPEED_COVERAGE:
        speed = np.nan_to_num(stream.speed, nan=0.0)
        moving_s = int(np.sum(speed > 0.5))
        dist = float(np.nanmax(stream.distance)) if stream.coverage("distance") > 0.5 else float(speed.sum())
        if moving_s and dist > 200:
            m["avg_pace_s_km"] = rnd(moving_s / dist * 1000)
            ngp = normalized_graded_pace(stream)
            if ngp:
                m["ngp_s_km"] = rnd(ngp)
                if m.get("avg_hr"):
                    m["ef"] = round(1000 / ngp * 60 / m["avg_hr"], 3)  # m/min per beat
                thr = th.get("run_threshold_s_km")
                if thr:
                    m["threshold_used_s_km"] = thr
                    m["if"] = round(thr / ngp, 2)
                    m["tss"] = rnd(r_tss(moving_s, ngp, thr))
                    m["tss_source"] = "pace"
            if stream.coverage("hr") > 0.5:
                m["decoupling"] = decoupling(graded_speed(stream), hr)

    lthr = lthr_for(sport, th)
    if "tss" not in m and sport in ("bike", "run", "multisport") and lthr and stream.coverage("hr") > 0.5:
        # hrTSS from the stream: mean of (HR/LTHR)^2 per second, so intervals count more than their average HR.
        ratio = hr[~np.isnan(hr)] / lthr
        m["tss"] = rnd(stream.seconds / 3600 * float(np.mean(ratio**2)) * 100)
        m["tss_source"] = "hr_est" if _lthr_estimated(th) else "hr"
    return m


def lap_table(stream: Stream, sport: str, th: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    power = np.nan_to_num(stream.power, nan=0.0)
    for i, lap in enumerate(stream.laps, 1):
        km = lap["total_distance"] / 1000 if lap.get("total_distance") else None
        t = lap.get("total_timer_time")
        row: dict[str, Any] = {
            "lap": i,
            "time_s": rnd(t),
            "km": rnd(km, 2) if km else None,
            "pace_s_km": rnd(t / km) if sport == "run" and km and t else None,
            "pace_s_100m": rnd(t / km / 10) if sport == "swim" and km and t else None,
            "kmh": rnd(km / t * 3600, 1) if sport == "bike" and km and t else None,
            "avg_power": lap.get("avg_power") if sport == "bike" else None,
            "avg_hr": lap.get("avg_heart_rate"),
            "max_hr": lap.get("max_heart_rate"),
            # FIT stores running cadence per leg; show steps per minute.
            "cadence": lap["avg_cadence"] * 2 if sport == "run" and (lap.get("avg_cadence") or 999) < 120
            else lap.get("avg_cadence"),
            "ascent_m": lap.get("total_ascent"),
        }
        rows = lap.get("_rows")
        if sport == "bike" and rows and rows[1] - rows[0] >= 30 and stream.coverage("power") >= _MIN_POWER_COVERAGE:
            np_w = normalized_power(power[rows[0]:rows[1]])
            row["np"] = rnd(np_w)
            if th.get("ftp") and np_w:
                row["pct_ftp"] = rnd(100 * (lap.get("avg_power") or 0) / th["ftp"])
        if sport == "run" and row["pace_s_km"] and th.get("run_threshold_s_km"):
            row["pct_threshold_pace"] = rnd(100 * th["run_threshold_s_km"] / row["pace_s_km"])
        out.append({k: v for k, v in row.items() if v is not None})
    return out


# ---------------------------------------------------------------- interval detection

# Power band (fraction of FTP) and shortest rep that counts, per planned session type. Without a plan (or for
# endurance-type sessions) any sustained block above Z2 counts.
_INTERVAL_BANDS = {
    "tempo": (0.76, 0.92, 600),
    "sweet_spot": (0.84, 0.97, 480),
    "threshold": (0.91, 1.08, 300),
    "vo2max": (1.03, 1.35, 120),
    "anaerobic": (1.15, 2.50, 30),
    "race": (0.76, None, 300),
    "test": (0.85, None, 180),
}
_GENERIC_BAND = (0.76, None, 300)
_MERGE_GAP_S = 20      # coasting, a traffic stop or a gear change inside a rep
_EDGE_TRIM_MAX_S = 45  # never trim more than this off either end of a rep
_HYSTERESIS = 0.08     # fraction of FTP a rep may sag below the band before it ends
_PCT_RANGE_RE = re.compile(r"(\d{2,3})\s*[-–]\s*(\d{2,3})\s*%")
_WATT_RANGE_RE = re.compile(r"(\d{2,4})\s*[-–]\s*(\d{2,4})\s*W\b")
_REPS_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(min|'|s\b|sec)", re.IGNORECASE)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index pairs of consecutive True values."""
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist()))


def interval_band(ftp: float, planned: dict[str, Any] | None = None) -> dict[str, Any]:
    """Watt band and minimum rep length to look for: from the planned targets (% FTP or watts), else the planned
    session type, else generic sustained work above Z2. Rep length "NxM min" in the title or targets sets the
    minimum to 60% of one rep, so a 4x10 still counts a rep cut short by a red light."""
    lo, hi, min_s = _GENERIC_BAND
    basis = "generic: sustained above Z2 (>75% FTP)"
    if planned:
        kind = planned.get("type")
        if kind in _INTERVAL_BANDS:
            lo, hi, min_s = _INTERVAL_BANDS[kind]
            basis = f"planned {kind.replace('_', ' ')}"
        text = " ".join(str(planned.get(k) or "") for k in ("title", "targets", "structure"))
        pct, watts = _PCT_RANGE_RE.search(text), _WATT_RANGE_RE.search(text)
        if pct and 50 <= int(pct.group(1)) < int(pct.group(2)) <= 250:
            lo, hi = int(pct.group(1)) / 100 - 0.05, int(pct.group(2)) / 100 + 0.06
            basis += f" targets {pct.group(1)}-{pct.group(2)}% FTP"
        elif watts and 0.4 * ftp <= int(watts.group(1)) < int(watts.group(2)) <= 2.5 * ftp:
            lo, hi = int(watts.group(1)) / ftp - 0.05, int(watts.group(2)) / ftp + 0.06
            basis += f" targets {watts.group(1)}-{watts.group(2)} W"
        reps = _REPS_RE.search(text)
        if reps:
            rep_s = float(reps.group(2).replace(",", ".")) * (1 if reps.group(3).lower().startswith("s") else 60)
            min_s = max(20, round(0.6 * rep_s))
            basis += f", reps {reps.group(1)}x{reps.group(2)}{'s' if rep_s < 60 else ' min'}"
    return {"basis": basis, "lo_w": lo * ftp, "hi_w": hi * ftp if hi else None, "min_s": int(min_s)}


def _interval_row(stream: Stream, i0: int, i1: int, ftp: float) -> dict[str, Any]:
    power = np.nan_to_num(stream.power[i0:i1], nan=0.0)
    hr = stream.hr[i0:i1]
    cad = stream.cadence[i0:i1]
    avg = float(power.mean())
    np_w = normalized_power(power) if len(power) >= 30 else None
    row: dict[str, Any] = {
        "start_min": round(i0 / 60, 1),
        "duration": f"{(i1 - i0) // 60}:{(i1 - i0) % 60:02d}",
        "duration_s": i1 - i0,
        "avg_power": rnd(avg),
        "np": rnd(np_w),
        "pct_ftp": rnd(100 * avg / ftp),
    }
    if np.mean(~np.isnan(hr)) > 0.8:
        half = (i1 - i0) // 2
        h1, h2 = float(np.nanmean(hr[:half])), float(np.nanmean(hr[half:]))
        p1, p2 = float(power[:half].mean()), float(power[half:].mean())
        row |= {"avg_hr": rnd(np.nanmean(hr)), "hr_first_half": rnd(h1), "hr_second_half": rnd(h2)}
        if p1 and h1 and h2:
            # Pw:HR drift: output per heartbeat, 2nd half vs 1st (positive = HR rose relative to power).
            row["pw_hr_drift_pct"] = round(((p1 / h1) - (p2 / h2)) / (p1 / h1) * 100, 1)
    pedaling = cad[~np.isnan(cad) & (cad > 0)]
    if len(pedaling):
        row["avg_cadence"] = rnd(pedaling.mean())
    return {k: v for k, v in row.items() if v is not None}


def detect_intervals(stream: Stream, ftp: float | None, planned: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Work intervals found in the 1 Hz power stream, whether or not the athlete pressed lap.

    A 30 s centered rolling mean (10 s for short reps) enters a block inside the band and leaves it only below the
    band minus _HYSTERESIS; blocks closer than _MERGE_GAP_S are merged (coasting, stops), ramp-in/out seconds below
    the band are trimmed off the edges (on a 5 s mean), and blocks shorter than the minimum rep are dropped."""
    if not ftp or stream.coverage("power") < _MIN_POWER_COVERAGE or stream.seconds < 120:
        return None
    band = interval_band(ftp, planned)
    lo, hi, min_s = band["lo_w"], band["hi_w"], band["min_s"]
    power = np.nan_to_num(stream.power, nan=0.0)
    window = 10 if min_s < 90 else 30
    smooth = np.convolve(power, np.ones(window) / window, mode="same")
    # Hysteresis: a rep is entered inside the band but only left once power falls clearly below it, so terrain
    # dips on an outdoor rep don't split it. Blocks must still reach the band and average close to it.
    floor = lo - _HYSTERESIS * ftp
    ceiling = hi + _HYSTERESIS * ftp / 2 if hi else None
    strict = (smooth >= lo) & ((smooth <= hi) if hi else True)
    loose = (smooth >= floor) & ((smooth <= ceiling) if ceiling else True)

    merged: list[list[int]] = []
    for i0, i1 in _runs(loose):
        if merged and i0 - merged[-1][1] < _MERGE_GAP_S:
            merged[-1][1] = i1
        else:
            merged.append([i0, i1])

    short = np.convolve(power, np.ones(5) / 5, mode="same")
    reps = []
    for i0, i1 in merged:
        if i1 - i0 < min_s or not strict[i0:i1].any():
            continue
        a, b = i0, i1
        while a < b - 1 and a - i0 < _EDGE_TRIM_MAX_S and short[a] < lo:
            a += 1
        while b - 1 > a and i1 - b < _EDGE_TRIM_MAX_S and short[b - 1] < lo:
            b -= 1
        if b - a >= min_s and power[a:b].mean() >= lo - _HYSTERESIS * ftp / 2:
            reps.append(_interval_row(stream, a, b, ftp))
    out: dict[str, Any] = {"basis": band["basis"], "band_w": [rnd(lo), rnd(hi) if hi else None],
                           "min_rep_s": min_s, "count": len(reps), "reps": reps}
    if len(reps) >= 2:
        first, last = reps[0], reps[-1]
        out["fade_pct"] = round((last["avg_power"] - first["avg_power"]) / first["avg_power"] * 100, 1)
        if first.get("avg_hr") and last.get("avg_hr"):
            out["hr_rise_bpm"] = rnd(last["avg_hr"] - first["avg_hr"])
    return out


def summary_metrics(sport: str, s: dict[str, Any], th: dict[str, Any], provider: str | None = None) -> dict[str, Any]:
    """Metrics from the activity summary alone (no FIT stream, or as the TSS fallback).

    The watch's own load stands in for TSS only for COROS (its Training Load is TSS-like). Garmin's activity
    training load is EPOC-based and not comparable, but Garmin's own power TSS is used when there is one."""
    duration = num(s.get("duration_s")) or 0
    dist = num(s.get("distance_m"))
    m: dict[str, Any] = {
        "recorded_s": rnd(duration),
        "avg_hr": rnd(s.get("avg_hr")),
        "max_hr": rnd(s.get("max_hr")),
        "avg_power": rnd(s.get("avg_power")) if sport == "bike" else None,
        "max_power": rnd(s.get("max_power")) if sport == "bike" else None,
        "np": rnd(s.get("np")) if sport == "bike" else None,
        "avg_cadence": rnd(s.get("avg_cadence")) if sport in ("bike", "run") else None,
    }
    if sport == "bike" and s.get("mmp"):
        m["mmp"] = s["mmp"]
    if m["avg_power"] and duration:
        m["kj"] = rnd(m["avg_power"] * duration / 1000)
    if sport == "run" and dist and duration:
        m["avg_pace_s_km"] = rnd(duration / dist * 1000)
    if sport == "swim" and dist and duration:
        m["avg_pace_s_100m"] = rnd(duration / dist * 100)

    np_w = num(s.get("np"))
    lthr = lthr_for(sport, th)
    if sport == "bike" and np_w and th.get("ftp"):
        m["ftp_used"] = th["ftp"]
        m["if"] = round(np_w / th["ftp"], 2)
        m["tss"], m["tss_source"] = rnd(tss_from_np(np_w, duration, th["ftp"])), "power_summary"
    elif sport == "swim" and m.get("avg_pace_s_100m") and th.get("css_s_100m"):
        m["if"] = round(th["css_s_100m"] / m["avg_pace_s_100m"], 2)
        m["tss"], m["tss_source"] = rnd(s_tss(duration, m["avg_pace_s_100m"], th["css_s_100m"])), "swim_pace"
    elif num(s.get("device_tss")):
        m["tss"], m["tss_source"] = rnd(s["device_tss"]), "garmin"
    elif provider == "coros" and num(s.get("training_load")) is not None:
        m["tss"], m["tss_source"] = rnd(s["training_load"]), "coros"
    elif lthr and m["avg_hr"] and duration:
        m["tss"], m["tss_source"] = rnd(hr_tss(duration, m["avg_hr"], lthr)), "hr_est" if _lthr_estimated(th) else "hr"
    return {k: v for k, v in m.items() if v is not None}


# ---------------------------------------------------------------- Performance Management Chart

def pmc(daily_tss: dict[str, float], start: date, end: date, seed_days: int = 120) -> list[dict[str, Any]]:
    """CTL (42d) / ATL (7d) exponentially weighted load. TSB is form going *into* the day (yesterday's CTL-ATL)."""
    k_ctl, k_atl = 1 - math.exp(-1 / 42), 1 - math.exp(-1 / 7)
    ctl = atl = 0.0
    out = []
    d = start - timedelta(days=seed_days)
    history: list[float] = []
    while d <= end:
        tss = daily_tss.get(d.isoformat(), 0.0)
        tsb = ctl - atl
        ctl += (tss - ctl) * k_ctl
        atl += (tss - atl) * k_atl
        history.append(ctl)
        if d >= start:
            out.append({
                "date": d.isoformat(), "tss": rnd(tss), "ctl": rnd(ctl, 1), "atl": rnd(atl, 1), "tsb": rnd(tsb, 1),
                "ramp_7d": rnd(ctl - history[-8], 1) if len(history) > 7 else None,
            })
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- FTP / CP estimation

def critical_power(mmp: dict[str, float]) -> dict[str, Any] | None:
    """2-parameter CP model fitted by linear regression of work vs time on 3-20 min bests."""
    points = [(int(d), p) for d, p in mmp.items() if 180 <= int(d) <= 1200 and p]
    if len(points) < 3:
        return None
    t = np.array([d for d, _ in points], dtype=float)
    work = np.array([d * p for d, p in points], dtype=float)
    slope, intercept = np.polyfit(t, work, 1)
    if slope <= 0 or intercept <= 0:
        return None
    return {"cp_w": rnd(slope), "w_prime_kj": rnd(intercept / 1000, 1), "durations_used_s": [int(x) for x in t]}


def ftp_estimates(mmp: dict[str, float]) -> dict[str, Any]:
    est: dict[str, Any] = {}
    if mmp.get("1200"):
        est["ftp_from_20min"] = rnd(0.95 * mmp["1200"])
    if mmp.get("3600"):
        est["best_60min"] = rnd(mmp["3600"])
    if mmp.get("480"):
        est["ftp_from_8min"] = rnd(0.90 * mmp["480"])
    cp = critical_power(mmp)
    if cp:
        est["critical_power"] = cp
    return est
