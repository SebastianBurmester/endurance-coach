"""Command line: setup, login, set-key, sync, auto, probe, status, serve, web."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path


def _setup(args: argparse.Namespace) -> None:
    """Choose source and focus, and write the machine-specific Claude Code files for this checkout."""
    from . import settings
    from .config import ROOT

    current = settings.save(**{k: v for k, v in (("provider", args.source), ("focus", args.focus)) if v})
    python = Path(sys.executable).as_posix()
    (ROOT / ".mcp.json").write_text(json.dumps({"mcpServers": {"coach": {
        "type": "stdio", "command": python, "args": ["-m", "endurance_coach", "serve"]}}}, indent=2) + "\n",
        encoding="utf-8")
    local = ROOT / ".claude" / "settings.local.json"
    local.parent.mkdir(exist_ok=True)
    local.write_text(json.dumps({"enabledMcpjsonServers": ["coach"], "enableAllProjectMcpServers": True}, indent=2) + "\n",
                     encoding="utf-8")
    print(json.dumps({"settings": current, "wrote": [".mcp.json", ".claude/settings.local.json", "FOCUS.local.md"]},
                     indent=2))


def _login(args: argparse.Namespace) -> None:
    from . import coros_api

    print(json.dumps(coros_api.login(open_browser=not args.no_browser), indent=2))


def _set_key(_: argparse.Namespace) -> None:
    from . import garmin_api
    from .config import GARMIN_KEY_FILE

    garmin_api.save_key(getpass.getpass("Paste your Garmin Personal API key (input hidden): "))
    print(f"Key accepted by Garmin and saved to {GARMIN_KEY_FILE}")


def _sync(args: argparse.Namespace) -> None:
    from . import sync

    try:
        print(json.dumps(sync.sync_all(args.days, args.max_fit), indent=2))
    except sync.SyncBusy as exc:
        print(exc)  # the sync timer just skips this run
    except sync.NotConnected as exc:
        sys.exit(str(exc))


def _auto(args: argparse.Namespace) -> None:
    from . import auto

    only = {k for k in ("checkin", "review") if getattr(args, k)} or None
    print(json.dumps(auto.run(only, args.dry_run), indent=2, default=str))


def _probe_coros() -> None:
    """Save one raw text response per COROS tool to check the formats the parsers expect."""
    from . import coros_api
    from .config import SAMPLES_DIR

    today = date.today()
    ymd = lambda d: d.strftime("%Y%m%d")  # noqa: E731
    week = ymd(today - timedelta(days=6))
    calls = {
        "user": ("queryUserInfo", {}),
        "records": ("querySportRecords", {"startDate": ymd(today - timedelta(days=30)), "endDate": ymd(today)}),
        "hrv": ("querySleepHrv", {"startDate": week, "endDate": ymd(today)}),
        "rhr": ("queryRestingHeartRate", {"days": 7}),
        "sleep": ("querySleepOverview", {"startDate": week, "endDate": ymd(today)}),
        "daily": ("queryDailyHealthData", {"days": 7}),
        "load": ("queryTrainingLoadAssessment", {"days": 7}),
        "recovery": ("queryRecoveryStatus", {}),
        "fitness": ("queryFitnessAssessmentOverview", {}),
        "schedule": ("queryTrainingSchedule", {}),
    }
    for name, (tool, arguments) in calls.items():
        try:
            (SAMPLES_DIR / f"{name}.txt").write_text(coros_api.call(tool, **arguments), encoding="utf-8")
            print(f"ok    {name}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {str(exc)[:160]}")
    (SAMPLES_DIR / "tools.json").write_text(json.dumps(coros_api.list_tools(), indent=2, ensure_ascii=False),
                                            encoding="utf-8")


def _probe_garmin() -> None:
    """Save one sample JSON response per Garmin endpoint to check field names."""
    from . import garmin_api
    from .config import SAMPLES_DIR

    today = date.today()
    week = today - timedelta(days=6)
    calls = {
        "profile": ("get_user_profile_settings", ()),
        "daily_summaries": ("get_daily_summaries", (week, today)),
        "hrv_summaries": ("get_daily_hrv_summaries", (week, today)),
        "sleep_summaries": ("get_daily_sleep_summaries", (week, today)),
        "sleep_day": ("get_daily_sleep", (today,)),
        "body_battery": ("get_daily_body_battery", (week, today)),
        "stress": ("get_daily_stress", (week, today)),
        "weight": ("get_weight_measurements", (today - timedelta(days=60), today)),
        "heart_rate_zones": ("get_heart_rate_zones", ()),
        "activities": ("search_activities", ()),
    }
    for name, (method, call_args) in calls.items():
        try:
            data = garmin_api.call(method, *call_args)
            (SAMPLES_DIR / f"{name}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            print(f"ok    {name}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {str(exc)[:160]}")


def _probe(_: argparse.Namespace) -> None:
    from . import settings
    from .config import SAMPLES_DIR

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    provider = settings.provider()
    if provider is None:
        sys.exit("No data source chosen: run `coach setup --source garmin|coros` or use the Settings tab.")
    (_probe_coros if provider == "coros" else _probe_garmin)()
    print(f"Samples in {SAMPLES_DIR}")


def _status(_: argparse.Namespace) -> None:
    from . import coach, coros_api, garmin_api, settings, store

    with store.connect() as conn:
        counts = {r[0]: r[1] for r in conn.execute("SELECT fit_status, COUNT(*) FROM activities GROUP BY fit_status")}
        sports = {r[0]: r[1] for r in conn.execute("SELECT sport, COUNT(*) FROM activities GROUP BY sport")}
        health = {r[0]: r[1] for r in conn.execute("SELECT source, COUNT(*) FROM daily GROUP BY source")}
        span = conn.execute("SELECT MIN(date), MAX(date) FROM activities").fetchone()
    provider = settings.provider()
    auth = coros_api.auth_status() if provider == "coros" else garmin_api.auth_status() if provider == "garmin" else None
    print(json.dumps({
        "app": settings.describe(), "auth": auth, "cache_from": store.get_meta("cache_provider"),
        "activities_by_sport": sports, "activities_by_fit_status": counts, "activity_span": list(span),
        "health_days_by_source": health, "thresholds": coach.thresholds(date.today()), "load_today": coach.load_today(),
    }, indent=2))


def _web(args: argparse.Namespace) -> None:
    from .webui import serve

    serve(args.host, args.port)


def _serve(_: argparse.Namespace) -> None:
    from .server import main as serve

    serve()


def main() -> None:
    parser = argparse.ArgumentParser(prog="coach")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup", help="choose source/focus and write .mcp.json for Claude Code in this checkout")
    p.add_argument("--source", choices=["garmin", "coros"])
    p.add_argument("--focus", choices=["road_cycling", "triathlon"])
    p.set_defaults(fn=_setup)
    p = sub.add_parser("login", help="log in to COROS (browser on this machine; the Settings tab works remotely)")
    p.add_argument("--no-browser", action="store_true", help="only print the URL (e.g. via an SSH tunnel to the Pi)")
    p.set_defaults(fn=_login)
    sub.add_parser("set-key", help="store this athlete's Garmin Personal API key").set_defaults(fn=_set_key)
    p = sub.add_parser("sync", help="sync activities, FIT files and health data from the connected source")
    p.add_argument("--days", type=int, default=None, help="re-fetch window in days (first run defaults to 365)")
    p.add_argument("--max-fit", type=int, default=None,
                   help="max FIT files to download this run (default: Garmin 100, COROS 40 of its 50/day)")
    p.set_defaults(fn=_sync)
    p = sub.add_parser("auto", help="unattended check-in / session reviews via headless Claude (after each sync)")
    p.add_argument("--checkin", action="store_true", help="only the check-in (overrides COACH_AUTO)")
    p.add_argument("--review", action="store_true", help="only session reviews (overrides COACH_AUTO)")
    p.add_argument("--dry-run", action="store_true", help="show what is due without starting Claude")
    p.set_defaults(fn=_auto)
    sub.add_parser("probe", help="dump raw source responses to data/samples").set_defaults(fn=_probe)
    sub.add_parser("status", help="show settings, login, cache contents and current load").set_defaults(fn=_status)
    sub.add_parser("serve", help="run the MCP server on stdio").set_defaults(fn=_serve)
    p = sub.add_parser("web", help="run the web dashboard (with the Settings tab)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (use the Tailscale IP; there is no auth)")
    p.add_argument("--port", type=int, default=8090)
    p.set_defaults(fn=_web)
    args = parser.parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    args.fn(args)


if __name__ == "__main__":
    main()
