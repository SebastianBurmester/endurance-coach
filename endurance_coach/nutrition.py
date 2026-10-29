"""Body weight and fueling: weight trend vs a target, W/kg and what a kilo is worth on the key climbs, daily carb
and protein targets from the plan, fueling per session and per race, and energy spent in training.

Numbers follow sports-nutrition consensus (IOC / ACSM / ISSN): carbs 3-12 g/kg/day scaled to the day's training,
protein 1.6-2.2 g/kg (more in a deficit), 30-120 g carbs/h during sessions by duration and intensity, weight loss at
most ~0.5% of body weight per week and never on key days or in the last weeks before an A event. Energy figures are
estimates: bike work from power (1 kJ ≈ 1 kcal burned), everything else from the watch or typical rates.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from . import athlete, coach, health, plan, store
from .util import num, rnd, to_date

_TREND_ALPHA = 0.1          # exponential smoothing per day (a ~10-day trend), Hacker's-Diet style
_RATE_DAYS = 28             # rate of change from a regression over this window
_SAFE_LOSS_PCT = 0.5        # % of body weight per week that keeps training quality
_LOCK_DAYS_BEFORE_A = 21    # no deficit in the last three weeks before an A event
_PROTEIN = (1.6, 2.2)
_PROTEIN_DEFICIT = (2.0, 2.4)
_DEFAULT_BIKE_KG = 9.0      # bike, bottles, shoes, helmet
_HARD = {"threshold", "vo2max", "anaerobic", "race", "test", "sweet_spot"}
_KCAL_PER_KG_H = {"run": 10.0, "swim": 8.0, "strength": 5.0, "mobility": 3.0, "walk": 4.0, "other": 7.0}
_FATIGUE_FONDO = 0.82       # share of FTP held on the climbs of a long hilly event


class KeyClimb(BaseModel):
    name: str = Field(description="e.g. 'Flüela from Susch'")
    length_km: float
    gain_m: float
    event_date: str | None = Field(None, description="YYYY-MM-DD of the event the climb is part of")
    power_w: float | None = Field(None, description="Planned power on the climb; default from FTP and the event")
    altitude_m: float | None = Field(None, description="Average altitude of the climb (thinner air above 1500 m)")


# ---------------------------------------------------------------- weight log (manual weigh-ins)

def log_weight(weight_kg: float, day: str | date | None = None, note: str | None = None) -> dict[str, Any]:
    if weight_kg in (None, ""):
        raise ValueError("enter a weight in kg")
    kg = float(weight_kg)
    if not 30 <= kg <= 250:
        raise ValueError(f"{kg} kg does not look like a body weight")
    d = to_date(day).isoformat()
    if d > date.today().isoformat():
        raise ValueError("weigh-ins can't be in the future")
    with store.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO body_log(date, weight_kg, note, updated_at) VALUES (?, ?, ?, ?)",
                     (d, round(kg, 2), (note or "").strip() or None, datetime.now().isoformat(timespec="seconds")))
    return {"date": d, "weight_kg": round(kg, 2), "note": note}


def delete_weight(day: str) -> dict[str, Any]:
    with store.connect() as conn:
        n = conn.execute("DELETE FROM body_log WHERE date = ?", (to_date(day).isoformat(),)).rowcount
    return {"deleted": n}


def manual_weights(since: str | None = None) -> dict[str, dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute("SELECT date, weight_kg, note FROM body_log WHERE date >= ? ORDER BY date",
                            (since or "0000",)).fetchall()
    return {r["date"]: dict(r) for r in rows}


# ---------------------------------------------------------------- physics (climbing speed)

def _rho(altitude_m: float | None) -> float:
    return 1.225 * math.exp(-(altitude_m or 300) / 8500)


def _speed(power_w: float, mass_kg: float, grade: float, cda: float = 0.36, crr: float = 0.0045,
           rho: float = 1.15) -> float:
    """Steady speed (m/s) for a power on a gradient: gravity, rolling resistance, air, 2.5% drivetrain loss."""
    theta = math.atan(grade)
    wheel = power_w * 0.975

    def need(v: float) -> float:
        return mass_kg * 9.81 * v * (math.sin(theta) + crr * math.cos(theta)) + 0.5 * rho * cda * v**3

    lo, hi = 0.1, 30.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if need(mid) < wheel else (lo, mid)
    return (lo + hi) / 2


def climb_time_s(length_km: float, gain_m: float, power_w: float, mass_kg: float, altitude_m: float | None = None) -> float:
    grade = gain_m / (length_km * 1000) if length_km else 0
    return length_km * 1000 / _speed(power_w, mass_kg, grade, rho=_rho(altitude_m))


def _all_out_power(ftp: float, length_km: float, gain_m: float, mass_kg: float) -> float:
    """Power held for the climb's duration flat out: FTP scaled by a power law (≈ +5% at 20 min, -4% at 90 min)."""
    p = ftp
    for _ in range(4):
        minutes = climb_time_s(length_km, gain_m, p, mass_kg) / 60
        p = ftp * (60 / max(minutes, 3)) ** 0.07
    return p


# ---------------------------------------------------------------- weight trend

def _profile_numbers(profile: dict[str, Any]) -> dict[str, Any]:
    user = coach.meta_json("coros_user")
    birth = profile.get("birth_year") or (int(user["birthday"][:4]) if user.get("birthday") else None)
    sex = (profile.get("sex") or user.get("gender") or "").lower()[:1] or None
    return {"height_cm": num(profile.get("height_cm")) or num(user.get("height_cm")), "birth_year": birth,
            "sex": sex if sex in ("m", "f") else None,
            "bike_kg": num(profile.get("bike_kg")) or _DEFAULT_BIKE_KG}


def _weights(start: date, end: date) -> list[dict[str, Any]]:
    """Every day with a weigh-in: from the scale / watch, or logged by hand (a manual entry wins that day)."""
    manual = manual_weights(start.isoformat())
    out = []
    for r in health.rows(start, end):
        m = manual.get(r["date"])
        if m:
            out.append({"date": r["date"], "kg": m["weight_kg"], "source": "manual", "note": m.get("note")})
        elif r["weight_kg"]:
            out.append({"date": r["date"], "kg": r["weight_kg"], "source": "scale"})
    return out


def _smooth(points: list[dict[str, Any]], end: date) -> list[dict[str, Any]]:
    """Daily trend weight: exponential smoothing in calendar days, so a weigh-in after a gap counts for the days
    missed and the trend lags ~10 days whether the athlete weighs daily or weekly."""
    if not points:
        return []
    by_day = {p["date"]: p["kg"] for p in points}
    d, trend, gap, out = to_date(points[0]["date"]), points[0]["kg"], 0, []
    while d <= end:
        gap += 1
        kg = by_day.get(d.isoformat())
        if kg is not None:
            trend += (1 - (1 - _TREND_ALPHA) ** gap) * (kg - trend)
            gap = 0
        out.append({"date": d.isoformat(), "trend": round(trend, 2)})
        d += timedelta(days=1)
    return out


def _rate(points: list[dict[str, Any]], end: date, days: int = _RATE_DAYS) -> dict[str, Any] | None:
    """kg per week from a least-squares line through the weigh-ins of the last `days` days."""
    since = (end - timedelta(days=days - 1)).isoformat()
    pts = [((to_date(p["date"]) - end).days, p["kg"]) for p in points if p["date"] >= since]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < 10:
        return None
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return {"kg_per_week": round(slope * 7, 2), "weigh_ins": len(pts), "days": days}


def _next_event(profile: dict[str, Any], today: date, priorities: str = "A") -> dict[str, Any] | None:
    ev = [e for e in profile.get("events") or [] if e.get("date", "") >= today.isoformat()
          and str(e.get("priority") or "B").upper() in priorities]
    return min(ev, key=lambda e: e["date"]) if ev else None


def _weight(profile: dict[str, Any], th: dict[str, Any], today: date) -> dict[str, Any]:
    start = today - timedelta(days=365)
    points = _weights(start, today)
    trend = _smooth(points, today)
    rate = _rate(points, today)
    cur = trend[-1]["trend"] if trend else th["weight_kg"]
    last = points[-1] if points else None
    out: dict[str, Any] = {
        "points": points, "trend": trend[-180:], "trend_kg": cur, "latest": last, "rate": rate,
        "profile_kg": num(profile.get("weight_kg")),
        "weigh_ins_28d": sum(p["date"] >= (today - timedelta(days=27)).isoformat() for p in points),
        "safe_loss_kg_week": round(cur * _SAFE_LOSS_PCT / 100, 2) if cur else None,
    }
    for days in (30, 90):
        past = next((t["trend"] for t in trend if t["date"] == (today - timedelta(days=days)).isoformat()), None)
        out[f"change_{days}d"] = round(cur - past, 1) if cur and past else None

    target, by = num(profile.get("target_weight_kg")), profile.get("target_weight_date")
    a_event = _next_event(profile, today)
    lock = to_date(a_event["date"]) - timedelta(days=_LOCK_DAYS_BEFORE_A) if a_event else None
    deadline = to_date(by) if by else lock
    if target and cur:
        goal: dict[str, Any] = {"target_kg": target, "by": deadline.isoformat() if deadline else None,
                                "to_go_kg": round(cur - target, 1)}
        if deadline and deadline > today:
            weeks = (deadline - today).days / 7
            goal["weeks_left"] = round(weeks, 1)
            goal["needed_kg_week"] = round((target - cur) / weeks, 2)
            safe = out["safe_loss_kg_week"]
            if cur - target <= 0.3:
                goal["verdict"] = "at target: hold it"
            elif -goal["needed_kg_week"] > safe * 1.05:
                goal["verdict"] = "too steep: needs more than the safe rate"
                goal["reachable_kg"] = round(cur - safe * weeks, 1)
            elif rate is None:
                goal["verdict"] = "needs more weigh-ins to judge"
            elif rate["kg_per_week"] <= goal["needed_kg_week"] + 0.05:
                goal["verdict"] = "on track"
            else:
                goal["verdict"] = "behind: trend is slower than needed"
        out["goal"] = goal
    if lock:
        out["lock_from"] = lock.isoformat()
        out["a_event"] = {"date": a_event["date"], "name": a_event.get("name")}
        if rate and cur:  # the current trend until three weeks out (or the target), then held
            proj = cur + rate["kg_per_week"] * max(0, (lock - today).days) / 7
            out["projected_at_a_event"] = round(max(proj, target) if target and rate["kg_per_week"] < 0 else proj, 1)
    return out


# ---------------------------------------------------------------- energy

def _bmr(kg: float | None, p: dict[str, Any], today: date) -> tuple[float | None, str]:
    if not kg:
        return None, "unknown"
    if p["height_cm"] and p["birth_year"] and p["sex"]:
        age = today.year - int(p["birth_year"])
        return 10 * kg + 6.25 * p["height_cm"] - 5 * age + (5 if p["sex"] == "m" else -161), "Mifflin-St Jeor"
    return 22.0 * kg, "rough (22 kcal/kg; add height, birth year and sex to the profile)"


def activity_kcal(a: dict[str, Any]) -> tuple[float | None, str]:
    m, s = a["metrics"], a["summary"]
    if a["sport"] == "bike" and m.get("kj"):
        return float(m["kj"]), "power"
    if num(s.get("calories")):
        return float(s["calories"]), "watch"
    return None, "none"


def _carb_share(intensity: float | None) -> float:
    """Share of energy from carbohydrate by IF (crossover concept)."""
    if intensity is None:
        return 0.6
    return 0.5 if intensity < 0.65 else 0.6 if intensity < 0.75 else 0.7 if intensity < 0.85 else 0.8 if intensity < 0.95 else 0.9


def _energy(today: date, kg: float | None, p: dict[str, Any], weeks: int = 12) -> dict[str, Any]:
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    acts = [a for a in store.activities(since=start.isoformat(), until=today.isoformat()) if a["sport"] not in ("walk",)]
    bmr, bmr_basis = _bmr(kg, p, today)
    weekly: dict[str, dict[str, Any]] = {}
    long_sessions = []
    for a in acts:
        kcal, src = activity_kcal(a)
        monday = (to_date(a["date"]) - timedelta(days=to_date(a["date"]).weekday())).isoformat()
        hours = (num(a["summary"].get("duration_s")) or 0) / 3600
        w = weekly.setdefault(monday, {"kcal": 0.0, "hours": 0.0})
        w["kcal"] += kcal or 0
        w["hours"] += hours
        if hours >= 2 and a["sport"] in ("bike", "run", "multisport") and a["date"] >= (today - timedelta(days=56)).isoformat():
            intensity = num(a["metrics"].get("if"))
            burned = kcal * _carb_share(intensity) / 4 if kcal else None
            dec = (a["metrics"].get("decoupling") or {}).get("decoupling_pct")
            long_sessions.append({k: v for k, v in {
                "id": a["id"], "date": a["date"], "name": a["name"], "sport": a["sport"], "hours": round(hours, 1),
                "kcal": rnd(kcal), "kcal_source": src, "if": intensity,
                "carbs_burned_g": rnd(burned), "carbs_burned_g_h": rnd(burned / hours) if burned else None,
                "advised_g_h": _in_session_carbs(hours * 60, intensity is not None and intensity >= 0.8)[1],
                "decoupling_pct": dec, "fueling_flag": dec is not None and dec > 8,
            }.items() if v is not None})
    rows = []
    for monday in sorted(weekly):
        w = weekly[monday]
        days = 7 if to_date(monday) + timedelta(days=6) <= today else (today - to_date(monday)).days + 1
        rows.append({"week_of": monday, "training_kcal": rnd(w["kcal"]), "hours": round(w["hours"], 1),
                     "training_kcal_day": rnd(w["kcal"] / days),
                     "total_kcal_day": rnd(bmr * 1.4 + w["kcal"] / days) if bmr else None, "days": days})
    return {"bmr_kcal": rnd(bmr), "bmr_basis": bmr_basis, "daily_life_factor": 1.4, "weeks": rows,
            "long_sessions": long_sessions[::-1]}


# ---------------------------------------------------------------- fueling per session and per day

def _in_session_carbs(minutes: float, hard: bool, race: bool = False) -> tuple[int, int]:
    """Carbs per hour during a session: 0 under an hour, then 30-60, 60-90 and 80-120 g/h (trained gut) by length."""
    if minutes < 60:
        return (0, 30) if hard or race else (0, 0)
    if minutes < 90:
        return (30, 60) if not hard else (45, 60)
    if minutes < 150:
        return (60, 90)
    return (80, 120) if race or hard else (70, 90)


def _session_kcal(s: dict[str, Any], kg: float | None, ftp: float | None) -> float | None:
    minutes = num(s.get("duration_min"))
    if not minutes or s.get("type") == "rest" or s.get("sport") == "rest":
        return None
    intensity = plan.typical_if(s.get("type"))
    sport = s.get("sport") or "other"
    if sport in ("bike", "brick") and ftp:
        return ftp * intensity * minutes * 60 / 1000
    return (kg or 70) * _KCAL_PER_KG_H.get(sport, 7.0) * minutes / 60 * (intensity / 0.7)


def session_fuel(s: dict[str, Any], kg: float | None, ftp: float | None) -> dict[str, Any] | None:
    minutes = num(s.get("duration_min")) or 0
    if s.get("status") == "skipped" or s.get("type") == "rest" or s.get("sport") in ("rest", "mobility") or not minutes:
        return None
    hard = s.get("type") in _HARD
    lo, hi = _in_session_carbs(minutes, hard, s.get("type") == "race")
    kcal = _session_kcal(s, kg, ftp)
    hours = minutes / 60
    out: dict[str, Any] = {"date": s["date"], "title": s.get("title"), "sport": s.get("sport"), "type": s.get("type"),
                           "minutes": rnd(minutes), "kcal": rnd(kcal), "carbs_g_h": [lo, hi],
                           "carbs_total_g": [rnd(lo * hours), rnd(hi * hours)] if hi else None,
                           "fluid_ml_h": [500, 750] if minutes >= 60 else None,
                           "sodium_mg_h": [300, 600] if minutes >= 90 else None}
    if hard or minutes >= 120:
        out["before"] = "1-2 g/kg carbs 2-3 h before" + (f" ({rnd(kg)}-{rnd(2 * kg)} g)" if kg else "")
    if hard or minutes >= 90:
        out["after"] = "within 1-2 h: 0.3-0.4 g/kg protein and 1-1.2 g/kg carbs" + \
            (f" ({rnd(0.3 * kg)}-{rnd(0.4 * kg)} g protein, {rnd(kg)}-{rnd(1.2 * kg)} g carbs)" if kg else "")
    return {k: v for k, v in out.items() if v is not None}


def _day_carbs(active: list[dict[str, Any]]) -> tuple[float, float, str]:
    hours = sum((num(s.get("duration_min")) or 0) * (0.5 if s.get("sport") == "strength" else 1)
                for s in active if s.get("sport") != "mobility") / 60
    hard = any(s.get("type") in _HARD for s in active)
    if hours == 0:
        return 3, 5, "rest"
    if hours < 1:
        lo, hi = 4, 5
    elif hours < 2:
        lo, hi = 5, 7
    elif hours < 3:
        lo, hi = 6, 8
    elif hours < 4.5:
        lo, hi = 8, 10
    else:
        lo, hi = 10, 12
    if hard:
        lo, hi = max(lo, 6), max(hi, 8)
    return lo, hi, "high" if lo >= 8 else "moderate" if lo >= 5 else "light"


def day_plan(start: date, end: date, profile: dict[str, Any] | None = None, kg: float | None = None,
             ftp: float | None = None, losing: bool | None = None) -> list[dict[str, Any]]:
    """Carbs and protein per planned day, fueling per session, and whether a small deficit fits that day."""
    profile = profile or athlete.load()
    if kg is None or ftp is None:
        th = coach.thresholds(start)
        kg, ftp = kg or th["weight_kg"], ftp or th["ftp"]
    if losing is None:
        losing = bool(num(profile.get("target_weight_kg")) and kg and kg - float(profile["target_weight_kg"]) > 0.3)
    planned: dict[str, list[dict[str, Any]]] = {}
    for s in plan.sessions(start, end + timedelta(days=2)):
        planned.setdefault(s["date"], []).append(s)
    events = {e["date"]: e for e in profile.get("events") or [] if e.get("date")}
    taper = [(b["start_date"], b["end_date"]) for b in plan.blocks(start, end)
             if "taper" in f"{b.get('phase')} {b.get('name')}".lower()]
    a_event = _next_event(profile, start, "AB")
    out = []
    d = start
    while d <= end:
        key = d.isoformat()
        active = [s for s in planned.get(key, []) if s.get("status") != "skipped" and s.get("type") != "rest"]
        lo, hi, load = _day_carbs(active)
        tomorrow = [s for s in planned.get((d + timedelta(days=1)).isoformat(), []) if s.get("status") != "skipped"]
        tmr_key = (d + timedelta(days=1)).isoformat()
        big_tomorrow = any(s.get("type") in _HARD or (num(s.get("duration_min")) or 0) >= 180 for s in tomorrow)
        event_soon = next((events[k] for k in (tmr_key, (d + timedelta(days=2)).isoformat()) if k in events), None)
        hard_today = any(s.get("type") in _HARD for s in active)
        reasons: list[str] = []
        kind = "normal"
        if key in events:
            kind, lo, hi = "race", max(lo, 8), max(hi, 12)
            reasons.append(f"Race day: {events[key].get('name')}. Breakfast 1-4 g/kg carbs 3 h before the start.")
        elif event_soon and str(event_soon.get("priority") or "B").upper() in ("A", "B"):
            kind, lo, hi = "carb_load", 8, 12
            reasons.append(f"Carb load for {event_soon.get('name')}: 8-12 g/kg, low fibre, ~1-2 kg water weight is expected.")
        elif big_tomorrow:
            kind, lo, hi = "fuel_up", max(lo, 6), max(hi, 8)
            reasons.append("Key or long session tomorrow: top up glycogen tonight.")
        elif hard_today or load == "high":
            kind = "fuel_up"
            reasons.append("Fuel the work: carbs before, during and after.")
        days_to_a = (to_date(a_event["date"]) - d).days if a_event else None
        in_taper = any(lo_d <= key <= hi_d for lo_d, hi_d in taper)
        if losing and kind == "normal" and load in ("rest", "light") and not big_tomorrow and not in_taper \
                and not (days_to_a is not None and days_to_a <= _LOCK_DAYS_BEFORE_A):
            kind = "deficit_ok"
            reasons.append("Easy day: a 300-500 kcal deficit fits here (less fat and snacks, keep protein high).")
        elif losing and kind == "normal" and days_to_a is not None and days_to_a <= _LOCK_DAYS_BEFORE_A:
            reasons.append("Within 3 weeks of the event: eat at maintenance, no deficit.")
        prot = _PROTEIN_DEFICIT if losing else _PROTEIN
        out.append({k: v for k, v in {
            "date": key, "kind": kind, "load": load, "carbs_g_kg": [lo, hi],
            "carbs_g": [rnd(lo * kg), rnd(hi * kg)] if kg else None,
            "protein_g": [rnd(prot[0] * kg), rnd(prot[1] * kg)] if kg else None,
            "training_kcal": rnd(sum(_session_kcal(s, kg, ftp) or 0 for s in active)) or None,
            "sessions": [f for f in (session_fuel(s, kg, ftp) for s in active) if f],
            "notes": reasons,
        }.items() if v is not None})
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- events and climbs

_KM = re.compile(r"(\d+(?:[.,]\d+)?)\s*km\b", re.I)
_GAIN = re.compile(r"(\d[\d'.]*)\s*(?:hm|m\s*(?:elev|gain|d\+|climbing)|d\+)", re.I)


def event_course(e: dict[str, Any]) -> tuple[float | None, float | None]:
    """Distance (km) and climbing (m) of an event: explicit fields, else read from its name ('~117 km / ~2800 Hm')."""
    km, gain = num(e.get("distance_km")), num(e.get("elevation_m"))
    text = f"{e.get('name') or ''} {e.get('format') or ''}"
    if km is None and (m := _KM.search(text)):
        km = float(m.group(1).replace(",", "."))
    if gain is None and (m := _GAIN.search(text)):
        gain = float(re.sub(r"[^\d]", "", m.group(1)))
    return km, gain


def _is_tt(e: dict[str, Any]) -> bool:
    return bool(re.search(r"time trial|zeitfahren|\bTT\b|hill ?climb|bergrennen", f"{e.get('format')} {e.get('name')}", re.I))


def _event_duration_h(e: dict[str, Any], ftp: float | None, mass: float) -> float | None:
    if num(e.get("expected_duration_h")):
        return float(e["expected_duration_h"])
    km, gain = event_course(e)
    if not km or not ftp:
        return None
    if _is_tt(e) and gain and gain / (km * 1000) > 0.03:
        return climb_time_s(km, gain, _all_out_power(ftp, km, gain, mass), mass) / 3600
    climb_km = min(km, (gain or 0) / 65)  # ~6.5% average on the climbs
    descent_km = min(km - climb_km, climb_km * 0.9)
    flat_km = km - climb_km - descent_km
    climb_s = climb_time_s(climb_km, gain or 0, ftp * _FATIGUE_FONDO, mass) if climb_km else 0
    flat_s = flat_km * 1000 / _speed(ftp * 0.68, mass, 0, cda=0.30)
    return (climb_s + flat_s + descent_km / 40 * 3600) / 3600


def _climbs(profile: dict[str, Any], th: dict[str, Any], kg: float | None, p: dict[str, Any],
            today: date) -> list[dict[str, Any]]:
    ftp, target = th["ftp"], num(profile.get("target_weight_kg"))
    if not ftp or not kg:
        return []
    events = {e["date"]: e for e in profile.get("events") or [] if e.get("date", "") >= today.isoformat()}
    climbs = [dict(c) for c in profile.get("key_climbs") or [] if not c.get("event_date") or c["event_date"] >= today.isoformat()]
    named = {c.get("event_date") for c in climbs}
    for e in events.values():  # events with a hill-climb course but no climbs saved: the whole course is the climb
        km, gain = event_course(e)
        if e["date"] not in named and km and gain and _is_tt(e) and gain / (km * 1000) > 0.03:
            climbs.append({"name": e.get("name", "").split(" (")[0], "length_km": km, "gain_m": gain,
                           "event_date": e["date"], "from_event": True})
    out = []
    for c in climbs:
        length, gain = float(c["length_km"]), float(c["gain_m"])
        mass = kg + p["bike_kg"]
        e = events.get(c.get("event_date") or "")
        tt = bool(e and _is_tt(e))
        power = num(c.get("power_w")) or (_all_out_power(ftp, length, gain, mass) if tt or not e else ftp * _FATIGUE_FONDO)
        alt = num(c.get("altitude_m"))
        t_now = climb_time_s(length, gain, power, mass, alt)
        t_less = climb_time_s(length, gain, power, mass - 1, alt)
        row: dict[str, Any] = {
            "name": c["name"], "event_date": c.get("event_date"), "event": e.get("name") if e else None,
            "length_km": length, "gain_m": gain, "grade_pct": round(gain / length / 10, 1), "power_w": rnd(power),
            "power_basis": "saved" if num(c.get("power_w")) else "all-out from FTP" if tt or not e else f"{round(_FATIGUE_FONDO * 100)}% FTP (long event)",
            "wkg": round(power / kg, 2), "time_s": rnd(t_now), "per_kg_s": rnd(t_now - t_less, 0),
            "per_kg_w": round(power / mass, 1),  # on a steep climb 1 kg less is worth about power / system mass
            "from_event": c.get("from_event"),
        }
        if target and target < kg:
            t_target = climb_time_s(length, gain, power, target + p["bike_kg"], alt)
            row["time_at_target_s"] = rnd(t_target)
            row["saved_s"] = rnd(t_now - t_target)
            # Watts that would buy the same time at today's weight.
            lo_w, hi_w = power, power * 1.5
            for _ in range(40):
                mid = (lo_w + hi_w) / 2
                lo_w, hi_w = (mid, hi_w) if climb_time_s(length, gain, mid, mass, alt) > t_target else (lo_w, mid)
            row["equivalent_w"] = rnd(lo_w - power)
        out.append({k: v for k, v in row.items() if v is not None})
    return out


def _race_fuel(profile: dict[str, Any], th: dict[str, Any], kg: float | None, p: dict[str, Any],
               today: date) -> list[dict[str, Any]]:
    out = []
    for e in sorted(profile.get("events") or [], key=lambda x: x.get("date", "")):
        if e.get("date", "") < today.isoformat():
            continue
        km, gain = event_course(e)
        hours = _event_duration_h(e, th["ftp"], (kg or 75) + p["bike_kg"]) if km else num(e.get("expected_duration_h"))
        prio = str(e.get("priority") or "B").upper()
        row: dict[str, Any] = {"date": e["date"], "name": e.get("name"), "priority": prio, "format": e.get("format"),
                               "days_to_go": (to_date(e["date"]) - today).days, "distance_km": km, "elevation_m": gain,
                               "duration_h": round(hours, 1) if hours else None,
                               "duration_basis": "saved" if num(e.get("expected_duration_h")) else "estimated from course and FTP" if hours else None}
        if hours:
            lo, hi = _in_session_carbs(hours * 60, True, race=True)
            row["carbs_g_h"] = [lo, hi]
            row["carbs_total_g"] = [rnd(lo * hours), rnd(hi * hours)]
            row["fluid_l"] = [round(0.5 * hours, 1), round(0.75 * hours, 1)]
            row["bottles"] = math.ceil(hours) if hours >= 1 else None  # 750 ml bottles at the upper fluid rate
            row["sodium_mg_h"] = [300, 600] if hours >= 1.5 else None
            if hours >= 1.5 and kg and prio in ("A", "B"):
                row["carb_load"] = {"days": 2 if hours >= 2.5 else 1, "g_kg": [8, 12] if hours >= 2.5 else [7, 10],
                                    "g_day": [rnd((8 if hours >= 2.5 else 7) * kg), rnd((12 if hours >= 2.5 else 10) * kg)]}
            if kg:
                row["breakfast_g"] = [rnd(1 * kg), rnd(3 * kg)] if hours >= 1 else [rnd(1 * kg), rnd(1.5 * kg)]
        row["race_plan_has_fueling"] = bool(re.search(r"carb|kohlenhydrat|gel|bottle|flasche|g/h|fuel|verpfleg",
                                                      e.get("race_plan") or "", re.I))
        out.append({k: v for k, v in row.items() if v is not None})
    return out


# ---------------------------------------------------------------- overview

def strategy() -> dict[str, Any] | None:
    raw = store.get_meta("nutrition_plan")
    return json.loads(raw) if raw else None


def save_strategy(text: str | None = None, climbs: list[KeyClimb] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if text is not None:
        note = {"text": text.strip(), "updated": datetime.now().isoformat(timespec="minutes")}
        store.set_meta("nutrition_plan", json.dumps(note))
        out["strategy"] = note
    if climbs is not None:
        profile = athlete.load()
        profile["key_climbs"] = [c.model_dump(exclude_none=True) for c in climbs]
        athlete.save(profile)
        out["key_climbs"] = profile["key_climbs"]
    return out or {"error": "Nothing to save: give strategy text and/or climbs."}


def _findings(w: dict[str, Any], energy: dict[str, Any], climbs: list[dict[str, Any]], today: date,
              readiness: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    rate, goal, cur = w.get("rate"), w.get("goal"), w.get("trend_kg")
    if w["weigh_ins_28d"] < 6:
        out.append({"level": "improve", "title": "Too few weigh-ins for a trend",
                    "text": f"{w['weigh_ins_28d']} in the last 4 weeks. Weigh 3-4 mornings a week, after the toilet "
                            "and before eating or drinking; log it here if your scale doesn't sync. Day-to-day swings "
                            "of 0.5-1.5 kg are water and glycogen, only the trend counts."})
    if rate and cur:
        pct = -rate["kg_per_week"] / cur * 100
        hrv_low = any(f.startswith(("HRV 7-day", "Resting HR")) for f in readiness.get("flags") or [])
        if pct > 1.0 or (pct > _SAFE_LOSS_PCT and hrv_low):
            out.append({"level": "improve", "title": "Losing weight fast",
                        "text": f"{-rate['kg_per_week']:.2f} kg/week ({pct:.1f}% of body weight)"
                                + (" while HRV / resting HR are flagged" if hrv_low else "")
                                + ". Above ~0.5%/week, power, recovery and immunity suffer. Eat more on training days; "
                                  "persistent fatigue, low mood or disturbed sleep together with weight loss can mean "
                                  "low energy availability (RED-S): talk to a sports physician or dietitian."})
        elif rate["kg_per_week"] > 0.3 and not goal:
            out.append({"level": "info", "title": "Weight is rising",
                        "text": f"+{rate['kg_per_week']:.2f} kg/week over 4 weeks. Fine in an off-season or after an "
                                "injury break; otherwise check portion sizes on easy days."})
    if goal:
        v = goal.get("verdict", "")
        if v.startswith("too steep"):
            out.append({"level": "improve", "title": "Target is too steep for the time left",
                        "text": f"{goal['to_go_kg']} kg in {goal['weeks_left']} weeks needs {-goal['needed_kg_week']:.2f} kg/week; "
                                f"the safe rate is about {w['safe_loss_kg_week']:.2f}. Realistic by {goal['by']}: "
                                f"{goal['reachable_kg']} kg, or move the date."})
        elif v == "on track":
            out.append({"level": "good", "title": "On track for the weight target",
                        "text": f"Trend {rate['kg_per_week']:+.2f} kg/week, needed {goal['needed_kg_week']:+.2f}."})
        elif v.startswith("behind"):
            out.append({"level": "info", "title": "Slower than the target needs",
                        "text": f"Trend {rate['kg_per_week']:+.2f} kg/week, needed {goal['needed_kg_week']:+.2f}. "
                                "Use the deficit-OK days below; don't cut on key days."})
    if w.get("lock_from") and w["lock_from"] <= today.isoformat():
        out.append({"level": "info", "title": "Race period: hold weight",
                    "text": f"{w['a_event']['name']} on {w['a_event']['date']}: eat at maintenance from now and fuel every session."})
    prof = w.get("profile_kg")
    if prof and cur and abs(prof - cur) >= 1.0 and w["weigh_ins_28d"] >= 3:
        out.append({"level": "info", "title": "Profile weight is out of date",
                    "text": f"The profile says {prof} kg, the trend is {cur:.1f} kg. W/kg on the other tabs uses the "
                            "profile value: ask the coach to update it."})
    flagged = [s for s in energy["long_sessions"] if s.get("fueling_flag")]
    if flagged:
        out.append({"level": "improve", "title": "Long sessions with late drift",
                    "text": f"{len(flagged)} of {len(energy['long_sessions'])} sessions over 2 h in the last 8 weeks "
                            "drifted more than 8% (power or pace per heartbeat). Heat and pacing matter too, but "
                            "under-fueling is the usual cause: start eating in the first 20-30 min."})
    if climbs and any(c.get("per_kg_s") for c in climbs):
        c = max(climbs, key=lambda x: x.get("time_s", 0))
        out.append({"level": "info", "title": "What a kilo is worth",
                    "text": f"{c['name']}: about {c['per_kg_s']} s per kg at {c['power_w']} W "
                            f"({c['length_km']} km at {c['grade_pct']}%). The same time comes from roughly "
                            f"{c['per_kg_w']:.0f} W more power."})
    return out


def overview(today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    profile = athlete.load()
    th = coach.thresholds(today)
    p = _profile_numbers(profile)
    w = _weight(profile, th, today)
    kg = w["trend_kg"] or th["weight_kg"]
    target = num(profile.get("target_weight_kg"))
    losing = bool(target and kg and kg - target > 0.3 and not (w.get("lock_from") and w["lock_from"] <= today.isoformat()))
    energy = _energy(today, kg, p)
    climbs = _climbs(profile, th, kg, p, today)
    readiness = coach.readiness(today)
    ftp = th["ftp"]
    return {
        "today": today.isoformat(),
        "weight": w,
        "weight_kg_used": kg,
        "ftp": ftp,
        "wkg": round(ftp / kg, 2) if ftp and kg else None,
        "wkg_at_target": round(ftp / target, 2) if ftp and target else None,
        "losing": losing,
        "profile": {**p, "target_weight_kg": target, "target_weight_date": profile.get("target_weight_date")},
        "strategy": strategy(),
        "days": day_plan(today, today + timedelta(days=13), profile, kg, ftp, losing),
        "races": _race_fuel(profile, th, kg, p, today),
        "climbs": climbs,
        "energy": energy,
        "manual": list(manual_weights((today - timedelta(days=120)).isoformat()).values())[::-1][:20],
        "physics": {"bike_kg": p["bike_kg"], "cda": 0.36, "crr": 0.0045, "rho": 1.15, "drivetrain": 0.975},
        "findings": _findings(w, energy, climbs, today, readiness),
    }


def today_fuel(today: date | None = None) -> dict[str, Any] | None:
    """Today's carbs / protein and session fueling for the dashboard's Today card."""
    today = today or date.today()
    days = day_plan(today, today)
    return days[0] if days else None
