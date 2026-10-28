"""Parse the formatted text the COROS MCP tools return into plain dicts.

Parsers are lenient: unknown lines are ignored, missing values stay None. If COROS changes a format, run
`coros-coach probe` and compare with data/samples/*.txt.
"""

from __future__ import annotations

import json
import re
from typing import Any

_DATE = r"(\d{4}-\d{2}-\d{2})"
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def _num(text: str | None) -> float | None:
    if not text:
        return None
    m = _NUM.search(text.replace(",", "") if re.search(r"\d,\d{3}", text) else text)
    return float(m.group().replace(",", ".")) if m else None


def clock_s(text: str | None) -> float | None:
    """'1:06:40' / '45:17' -> seconds."""
    if not text:
        return None
    m = re.search(r"(\d+):(\d{2})(?::(\d{2}))?", text)
    if not m:
        return None
    a, b, c = m.groups()
    return int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)


def hm_min(text: str | None) -> float | None:
    """'7h 9min' / '23 min' / '36h' -> minutes."""
    if not text:
        return None
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*min", text)
    if not h and not m:
        return None
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def distance_m(text: str | None) -> float | None:
    if not text:
        return None
    value = _num(text)
    if value is None:
        return None
    return value * 1000 if re.search(r"\bkm\b", text) else value


def _pairs(block: str) -> dict[str, str]:
    """'Key: Value | Key2: Value2' lines -> {key: value} (first occurrence wins)."""
    out: dict[str, str] = {}
    for line in block.splitlines():
        for part in line.split(" | "):
            key, sep, value = part.strip().lstrip("|").strip().partition(":")
            if sep and key and key not in out:
                out[key.strip()] = value.strip()
    return out


def _dated_blocks(text: str, header: str) -> list[tuple[str, str]]:
    """Split text into (date, block) at lines matching `header` (a regex with one date group)."""
    matches = list(re.finditer(header, text, re.MULTILINE))
    return [(m.group(1), text[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)])
            for i, m in enumerate(matches)]


# ---------------------------------------------------------------- activities

def sport_records(text: str) -> list[dict[str, Any]]:
    out = []
    for m in re.finditer(r"^\d+\. (.+?) — " + _DATE + r"\s*$", text, re.MULTILINE):
        end = text.find("\n\n", m.end())
        block = text[m.end(): end if end > 0 else len(text)]
        kv = _pairs(block)
        window = re.search(r"startTimestamp=(\d+)\s*\|\s*endTimestamp=(\d+)", block)
        label = re.search(r"LabelId:\s*(\d+)", block)
        sport = re.search(r"SportType:\s*(\d+)", block)
        if not label or not sport:
            continue
        out.append({
            "label_id": int(label.group(1)),
            "sport_type": int(sport.group(1)),
            "type_name": m.group(1).strip(),
            "date": m.group(2),
            "location": kv.get("Location"),
            "start_ts": int(window.group(1)) if window else None,
            "end_ts": int(window.group(2)) if window else None,
            "duration_s": clock_s(kv.get("Duration")),
            "distance_m": distance_m(kv.get("Distance")),
            "avg_speed_kmh": _num(kv.get("Average Speed")),
            "avg_hr": _num(kv.get("Avg HR")),
            "calories": _num(kv.get("Calories")),
        })
    return out


_DETAIL_NUMERIC = {
    "Training Load": "training_load", "Aerobic TE": "aerobic_te", "Anaerobic TE": "anaerobic_te",
    "Average Heart Rate": "avg_hr", "Max Heart Rate": "max_hr", "Max Speed": "max_speed_kmh",
    "Moving Average Speed": "moving_avg_speed_kmh", "Average Power": "avg_power", "Normalized Power": "np",
    "Max Power": "max_power", "Average Cadence": "avg_cadence", "Water Temperature": "water_temp_c",
}


def activity_detail(text: str) -> dict[str, Any]:
    kv = _pairs(text)
    out: dict[str, Any] = {key: _num(kv.get(src)) for src, key in _DETAIL_NUMERIC.items() if kv.get(src)}
    if kv.get("Workout Time"):
        out["workout_time_s"] = clock_s(kv["Workout Time"])
    if kv.get("Total Time"):
        out["total_time_s"] = clock_s(kv["Total Time"])
    if kv.get("Average Pace"):
        out["avg_pace"] = kv["Average Pace"]
    gain_loss = kv.get("Elevation Gain / Loss")
    if gain_loss:
        nums = _NUM.findall(gain_loss)
        out["elevation_gain_m"] = float(nums[0]) if nums else None
        out["elevation_loss_m"] = float(nums[1]) if len(nums) > 1 else None
    for src, key in (("Training Focus", "training_focus"), ("Perceived Effort", "perceived_effort"),
                     ("Workout Note", "note"), ("Feel", "feel")):
        if kv.get(src):
            out[key] = kv[src]
    return out


def laps(text: str) -> list[dict[str, Any]]:
    """queryActivityLapData returns JSON: take the auto/manual lap group (type != 11 and != -1) with most laps."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    groups = [g for g in data.get("lapGroups") or data.get("lapList") or data.get("laps") or [] if isinstance(g, dict)]
    if not groups:
        groups = [g for g in data.values() if isinstance(g, list)
                  for g in g if isinstance(g, dict) and "laps" in g]
    groups = [g for g in groups if g.get("type") not in (-1, 11)] or groups
    best = max(groups, key=lambda g: len(g.get("laps") or []), default=None)
    rows = []
    for lap in (best or {}).get("laps") or []:
        dist_cm = lap.get("distance")
        rows.append({k: v for k, v in {
            "lap": lap.get("lapIndex"),
            "km": round(dist_cm / 100000, 2) if dist_cm else None,
            "time_s": lap.get("time"),
            "pace_s_km": lap.get("avgPace") or None,
            "gap_s_km": lap.get("adjustedPace") or None,
            "avg_hr": lap.get("avgHr") or None,
            "max_hr": lap.get("maxHr") or None,
            "avg_power": lap.get("avgPower") or None,
            "cadence": lap.get("avgCadence") or None,
            "ascent_m": lap.get("elevGain"),
        }.items() if v is not None})
    return rows


def fit_urls(text: str) -> dict[int, str]:
    out = {}
    for m in re.finditer(r"(\d+)\.fit\s*\n\s*(https?://\S+)", text):
        out[int(m.group(1))] = m.group(2)
    return out


# ---------------------------------------------------------------- daily health

def sleep_hrv(text: str) -> dict[str, dict[str, Any]]:
    """The official assessment section only (the raw time series below it is ignored)."""
    section = text.split("Sleep HRV Time Series")[0]
    out = {}
    for day, block in _dated_blocks(section, r"^" + _DATE + r":\s*$"):
        avg = re.search(r"HRV Avg:\s*(\d+(?:\.\d+)?)\s*ms(?:\s*\W\s*(.+))?", block)
        if not avg:
            continue
        rng = re.search(r"Normal Range:\s*(\d+)\s*-\s*(\d+)", block)
        base = re.search(r"Baseline:\s*(\d+)", block)
        out[day] = {"hrv": float(avg.group(1)), "evaluation": (avg.group(2) or "").strip() or None,
                    "normal_low": float(rng.group(1)) if rng else None,
                    "normal_high": float(rng.group(2)) if rng else None,
                    "baseline": float(base.group(1)) if base else None}
    return out


def resting_hr(text: str) -> dict[str, dict[str, Any]]:
    return {d: {"rhr": float(v)} for d, v in re.findall(r"^" + _DATE + r":\s*(\d+)\s*bpm", text, re.MULTILINE)}


def sleep_overview(text: str) -> dict[str, dict[str, Any]]:
    out = {}
    for day, block in _dated_blocks(text, r"^" + _DATE + r"\s*$"):
        kv = _pairs(block)
        rec = {
            "sleep_score": _num(kv.get("Sleep Score")),
            "main_sleep_min": hm_min(kv.get("Main Sleep (asleep)")),
            "daily_sleep_min": hm_min(kv.get("Daily Sleep")),
            "deep_pct": _num(kv.get("Deep Sleep Ratio")),
            "light_pct": _num(kv.get("Light Sleep Ratio")),
            "rem_pct": _num(kv.get("REM Ratio")),
            "awake_min": hm_min(kv.get("Awake Time")),
            "awake_count": _num(kv.get("Awake Count (>5 min)")),
            "naps_min": hm_min(kv.get("Naps Total") or kv.get("Naps Total (asleep)")),
            "window": kv.get("Main Sleep Window"),
        }
        if any(v is not None for v in rec.values()):
            out[day] = {k: v for k, v in rec.items() if v is not None}
    return out


def daily_health(text: str) -> dict[str, dict[str, Any]]:
    out = {}
    for compact, block in _dated_blocks(text, r"^--- (\d{8}) ---\s*$"):
        day = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
        steps = re.search(r"Steps:\s*([\d,]+)", block)
        exercise = re.search(r"Exercise:\s*([^|\n]+)", block)
        stress = re.search(r"Stress:\s*Avg\s*(\d+)", block)
        sleep_total = re.search(r"Total:\s*([^|\n]+)", block)
        sleep_hr = re.search(r"Sleep HR:\s*Avg\s*(\d+)\s*bpm\s*\|\s*Min\s*(\d+)", block)
        rec = {
            "steps": float(steps.group(1).replace(",", "")) if steps else None,
            "exercise_min": hm_min(exercise.group(1)) if exercise else None,
            "stress_avg": float(stress.group(1)) if stress else None,
            "sleep_total_min": hm_min(sleep_total.group(1)) if sleep_total else None,
            "sleep_hr_avg": float(sleep_hr.group(1)) if sleep_hr else None,
            "sleep_hr_min": float(sleep_hr.group(2)) if sleep_hr else None,
        }
        out[day] = {k: v for k, v in rec.items() if v is not None}
    return out


def training_load(text: str) -> dict[str, dict[str, Any]]:
    out = {}
    for day, block in _dated_blocks(text, r"^" + _DATE + r"\s*$"):
        kv = _pairs(block)
        out[day] = {k: v for k, v in {
            "comment": kv.get("Comment"), "short_term": _num(kv.get("Short-Term Load")),
            "long_term": _num(kv.get("Long-Term Load")), "ratio": _num(kv.get("Load Ratio")),
        }.items() if v is not None}
    return out


def menstruation(text: str) -> dict[str, Any]:
    """queryMenstruationCycles: today's status, the daily phase table and the cycle table (recorded and predicted)."""
    head = text.split("Daily Phase Status")[0]

    def line(label: str) -> str:
        m = re.search(r"^" + label + r":\s*(.+?)\s*$", head, re.MULTILINE)
        return m.group(1) if m else ""

    def first(pattern: str, value: str) -> str | None:
        m = re.search(pattern, value)
        return m.group(1) if m else None

    def count(pattern: str, value: str) -> int | None:
        found = first(pattern, value)
        return int(found) if found else None

    today, current, general, nxt = line("Today"), line("Current Cycle"), line("General"), line("Next Period Start")
    status: dict[str, Any] = {
        "today": first(_DATE, today),
        "today_status": first(r"\|\s*(.+)$", today),
        "cycle_day": count(r"cycle day\s*(\d+)", current),
        "current_period": " to ".join(re.findall(_DATE, current)) or None,
        "next_period_start": first(_DATE, nxt),
        "next_period_predicted": "predicted" in nxt if nxt else None,
        "usual_period_days": count(r"period\s*(\d+)\s*day", general),
        "usual_cycle_days": count(r"cycle\s*(\d+)\s*day", general),
    }
    days = {d: {"phase": p.strip()} for d, p in re.findall(r"^" + _DATE + r"\s*\|\s*([A-Za-z][^|\n]*?)\s*$",
                                                           text, re.MULTILINE)}
    cycles = []
    for start, end, kind, notes in re.findall(r"^" + _DATE + r"\s+to\s+" + _DATE + r"\s*\|\s*([^|\n]+?)\s*\|\s*(.*)$",
                                              text, re.MULTILINE):
        length = re.search(r"Cycle length\s*(\d+)", notes)
        cycles.append({"period_start": start, "period_end": end, "status": kind,
                       "cycle_length_days": int(length.group(1)) if length else None,
                       "notes": notes.strip() or None})
    return {"status": {k: v for k, v in status.items() if v is not None}, "days": days,
            "cycles": sorted(cycles, key=lambda c: c["period_start"])}


def recovery(text: str) -> dict[str, Any]:
    kv = _pairs(text)
    full = kv.get("Estimated Full Recovery")
    return {k: v for k, v in {
        "recovery_pct": _num(kv.get("Recovery")), "level": kv.get("Level"),
        "full_recovery_h": round(hm_min(full) / 60, 1) if hm_min(full) is not None else None,
    }.items() if v is not None}


def fitness(text: str) -> dict[str, Any]:
    kv = _pairs(text)
    out: dict[str, Any] = {"vo2max": _num(kv.get("VO2max")), "running_level": _num(kv.get("Running Level")),
                           "threshold_pace_s_km": clock_s(kv.get("Threshold Pace"))}
    preds = {k.replace(" Prediction", ""): v for k, v in kv.items() if k.endswith("Prediction")}
    if preds:
        out["race_predictions"] = preds
    return {k: v for k, v in out.items() if v is not None}


def user_info(text: str) -> dict[str, Any]:
    kv = _pairs(text)
    return {k: v for k, v in {
        "height_cm": _num(kv.get("Height")), "weight_kg": _num(kv.get("Weight")),
        "birthday": (kv.get("Birthday") or "")[:10] or None, "gender": kv.get("Gender"),
        "nickname": kv.get("Nickname"),
    }.items() if v is not None}
