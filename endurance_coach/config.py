"""Paths and constants."""

from __future__ import annotations

import os
from pathlib import Path

# One checkout = one athlete: settings, profile, cache and logins all live next to the code.
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("COACH_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "coach.db"
# Caches from before the Garmin and COROS coaches were merged; adopted by store.connect() on first use.
LEGACY_DBS = {"garmin": DATA_DIR / "garmin.db", "coros": DATA_DIR / "coros.db"}
FIT_DIR = DATA_DIR / "fit"
SAMPLES_DIR = DATA_DIR / "samples"
AUTH_DIR = Path(os.environ.get("COACH_AUTH_DIR", DATA_DIR / "auth"))
ATHLETE_FILE = Path(os.environ.get("COACH_ATHLETE", ROOT / "athlete.json"))
SETTINGS_FILE = Path(os.environ.get("COACH_SETTINGS", ROOT / "settings.json"))
# Imported by CLAUDE.md; points at the coaching guide for the chosen focus (written when the focus changes).
FOCUS_POINTER = ROOT / "FOCUS.local.md"

# Garmin Personal API key and its bearer-token cache, per athlete.
GARMIN_KEY_FILE = AUTH_DIR / "garmin-api-key"
GARMIN_TOKEN_FILE = AUTH_DIR / "garmin-bearer-token.json"
GARMIN_FIT_PER_RUN = 100

# OAuth client registration + tokens for the COROS MCP server. Keep it on one machine only: refresh tokens rotate.
COROS_AUTH_FILE = Path(os.environ.get("COROS_COACH_AUTH", AUTH_DIR / "coros.json"))
COROS_MCP_URL = os.environ.get("COROS_MCP_URL", "https://mcpeu.coros.com/mcp")
LOGIN_PORT = 8765
# COROS allows 50 FIT downloads per day; leave some for the athlete's own Claude chats that use the connector.
FIT_DAILY_LIMIT = 40

# Durations (seconds) tracked for the mean-maximal power curve.
MMP_DURATIONS = [1, 5, 10, 15, 30, 60, 120, 180, 300, 480, 600, 720, 1200, 1800, 2700, 3600, 5400, 7200]

# Coggan power zones as upper bounds in fraction of FTP.
POWER_ZONES = [
    ("Z1 Active recovery", 0.55),
    ("Z2 Endurance", 0.75),
    ("Z3 Tempo", 0.90),
    ("Z4 Threshold", 1.05),
    ("Z5 VO2max", 1.20),
    ("Z6 Anaerobic", 1.50),
    ("Z7 Neuromuscular", float("inf")),
]

# Friel cycling HR zones as upper bounds in fraction of bike LTHR (5a-c merged into Z5).
HR_ZONES_BIKE = [
    ("Z1 Recovery", 0.81),
    ("Z2 Aerobic", 0.90),
    ("Z3 Tempo", 0.94),
    ("Z4 Sub-threshold", 1.00),
    ("Z5 Supra-threshold", float("inf")),
]
# Friel running HR zones in fraction of run LTHR.
HR_ZONES_RUN = [
    ("Z1 Recovery", 0.85),
    ("Z2 Aerobic", 0.89),
    ("Z3 Tempo", 0.94),
    ("Z4 Sub-threshold", 1.00),
    ("Z5 Supra-threshold", float("inf")),
]

# Fallback HR zones in fraction of max HR when no LTHR is known.
HR_ZONES_MAX = [
    ("Z1", 0.60),
    ("Z2", 0.70),
    ("Z3", 0.80),
    ("Z4", 0.90),
    ("Z5", float("inf")),
]

# Activities that never count toward training load.
NON_TRAINING_SPORTS = {"mobility", "walk"}
# Sports whose original FIT file is downloaded and analyzed.
FIT_SPORTS = {"bike", "run", "swim", "multisport"}

# COROS health sources and how far back to backfill them on the first sync (COROS keeps ~30 days of load assessments).
HEALTH_BACKFILL_DAYS = {"rhr": 120, "hrv": 120, "sleep": 120, "daily": 120, "coros_load": 30}

# Garmin health endpoints: (source name, max days per request, default backfill days).
GARMIN_HEALTH_SOURCES = {
    "hrv": (180, 365),
    "daily": (28, 365),
    "sleep": (28, 365),
    "body_battery": (28, 365),
    "stress": (7, 28),  # daily summaries already carry average stress for the full year
    "weight": (90, 365),
}


def coros_sport_of(code: int | None) -> str:
    """Map a COROS sportType code to a coaching sport."""
    if code is None:
        return "other"
    if code in (100, 101, 102, 103):
        return "run"
    if 200 <= code < 300:
        return "bike"
    if code in (300, 301):
        return "swim"
    if code == 402:
        return "strength"
    if 10000 <= code <= 10003:
        return "multisport"
    if code in (904, 905):
        return "mobility"
    if code in (104, 900):
        return "walk"
    return "other"


_GARMIN_SPORTS = {
    "lap_swimming": "swim", "open_water_swimming": "swim", "swimming": "swim",
    "strength_training": "strength", "multi_sport": "multisport", "triathlon": "multisport", "duathlon": "multisport",
    "walking": "walk", "casual_walking": "walk", "speed_walking": "walk",
    "yoga": "mobility", "pilates": "mobility", "meditation": "mobility", "breathwork": "mobility",
    "mobility": "mobility", "stretching": "mobility",
}


def garmin_sport_of(type_key: str | None) -> str:
    """Map a Garmin activityType.typeKey to a coaching sport."""
    if not type_key:
        return "other"
    key = type_key.lower()
    if "cycl" in key or "biking" in key or key in {"virtual_ride", "bmx"}:
        return "bike"
    if "running" in key or key == "treadmill":
        return "run"
    return _GARMIN_SPORTS.get(key, "other")
