"""Per-athlete app settings (settings.json): which data source is connected and which sport the coaching focuses on."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from .config import FOCUS_POINTER, SETTINGS_FILE

Provider = Literal["garmin", "coros"]
Focus = Literal["road_cycling", "triathlon"]

PROVIDERS: dict[str, str] = {"garmin": "Garmin", "coros": "COROS"}
FOCUSES: dict[str, dict[str, str]] = {
    "road_cycling": {"label": "Road cycling", "title": "Cycling Coach", "guide": "coaching/road-cycling.md"},
    "triathlon": {"label": "Triathlon", "title": "Tri Coach", "guide": "coaching/triathlon.md"},
}
_DEFAULTS: dict[str, Any] = {"provider": None, "focus": "road_cycling"}


def load() -> dict[str, Any]:
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    out = {**_DEFAULTS, **{k: v for k, v in raw.items() if k in _DEFAULTS}}
    if out["provider"] not in PROVIDERS:
        out["provider"] = None
    if out["focus"] not in FOCUSES:
        out["focus"] = _DEFAULTS["focus"]
    return out


def save(**changes: Any) -> dict[str, Any]:
    current = load()
    if "provider" in changes and changes["provider"] not in (None, *PROVIDERS):
        raise ValueError(f"unknown data source {changes['provider']!r}")
    if "focus" in changes and changes["focus"] not in FOCUSES:
        raise ValueError(f"unknown focus {changes['focus']!r}")
    current.update({k: v for k, v in changes.items() if k in _DEFAULTS})
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)
    write_focus_pointer(current["focus"])
    return current


def write_focus_pointer(focus: str | None = None) -> None:
    """CLAUDE.md imports FOCUS.local.md, which imports the guide for the focus; new Claude sessions pick it up."""
    guide = FOCUSES[focus or load()["focus"]]["guide"]
    text = f"<!-- Written by the app from settings.json; change the focus in the dashboard's Settings tab. -->\n@{guide}\n"
    if not FOCUS_POINTER.exists() or FOCUS_POINTER.read_text(encoding="utf-8") != text:
        FOCUS_POINTER.write_text(text, encoding="utf-8")


def provider() -> str | None:
    return load()["provider"]


def provider_label(name: str | None = None) -> str:
    name = name or provider()
    return PROVIDERS.get(name or "", "no data source")


def focus() -> str:
    return load()["focus"]


def describe() -> dict[str, Any]:
    s = load()
    f = FOCUSES[s["focus"]]
    return {"provider": s["provider"], "provider_label": provider_label(s["provider"]), "focus": s["focus"],
            "focus_label": f["label"], "title": f["title"], "coaching_guide": f["guide"]}
