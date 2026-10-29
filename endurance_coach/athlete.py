"""Athlete profile (athlete.json): per-sport thresholds with history, goals, events. The coach's source of truth."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from .config import ATHLETE_FILE

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "",
    "weight_kg": None,
    "max_hr": None,
    # Dated threshold histories, e.g. {"date": "2026-11-02", "value": 205, "source": "ramp test"}.
    "ftp_history": [],             # bike FTP, W
    "run_threshold_history": [],   # run threshold pace, s/km
    "css_history": [],             # swim critical swim speed, s/100 m
    "lthr_bike": None,
    "lthr_run": None,
    "weekly_hours_available": None,
    "sleep_need_h": None,          # hours asleep; the sleep analysis assumes 8 without it
    # Nutrition tab: weight target (agreed with the athlete), body data for the resting energy estimate, bike
    # weight for climbing times, and key climbs [{"name", "length_km", "gain_m", "event_date", "power_w"}].
    "target_weight_kg": None,
    "target_weight_date": None,
    "height_cm": None,
    "birth_year": None,
    "sex": None,                   # "m" | "f"
    "bike_kg": None,               # bike + bottles + kit; 9 kg without it
    "key_climbs": [],
    "goals": [],
    "events": [],  # [{"date": "2027-06-20", "name": "Zurich 70.3", "priority": "A", "format": "70.3"}]
    "notes": "",
}

HISTORIES = ("ftp_history", "run_threshold_history", "css_history")


def load() -> dict[str, Any]:
    try:
        data = json.loads(ATHLETE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    # Profiles from the Garmin cycling coach: FTP entries had "ftp" instead of "value", and one LTHR (cycling).
    for e in data.get("ftp_history") or []:
        if "value" not in e and "ftp" in e:
            e["value"] = e.pop("ftp")
    if "lthr" in data:
        legacy_lthr = data.pop("lthr")
        data.setdefault("lthr_bike", legacy_lthr)
    return {**DEFAULT_PROFILE, **data}


def save(profile: dict[str, Any]) -> None:
    for key in HISTORIES:
        profile[key] = sorted(profile.get(key) or [], key=lambda e: e["date"])
    ATHLETE_FILE.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")


def value_on(history_key: str, day: date | str, profile: dict[str, Any] | None = None) -> float | None:
    """Threshold in effect on a day: the latest entry on/before it, else the earliest one."""
    history = (profile or load()).get(history_key) or []
    if not history:
        return None
    key = day.isoformat() if isinstance(day, date) else str(day)[:10]
    applicable = [e for e in history if e["date"] <= key]
    entry = applicable[-1] if applicable else history[0]
    return float(entry["value"])


def add_threshold(profile: dict[str, Any], history_key: str, value: float, day: str | None, source: str | None) -> None:
    entry = {"date": day or date.today().isoformat(), "value": value, "source": source or "manual"}
    profile[history_key] = [e for e in profile.get(history_key) or [] if e["date"] != entry["date"]] + [entry]


def pace_to_s(text: str | float | None) -> float | None:
    """'5:20' (min:s) -> 320 seconds; numbers pass through."""
    if text is None or isinstance(text, (int, float)):
        return text
    minutes, _, seconds = str(text).strip().partition(":")
    return int(minutes) * 60 + int(seconds or 0)


def s_to_pace(seconds: float | None) -> str | None:
    if not seconds:
        return None
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"
