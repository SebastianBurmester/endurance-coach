"""Sync from whichever data source the settings name (Garmin or COROS)."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import settings, store
from .config import DATA_DIR

_LOGGER = logging.getLogger(__name__)
_AUTO_SYNC_INTERVAL_S = 30 * 60
_AUTO_SYNC_FIT = {"garmin": 15, "coros": 10}


class NotConnected(RuntimeError):
    """No data source chosen, or its login/key is missing or rejected."""


class SyncBusy(RuntimeError):
    """Another sync of this athlete (sync timer, dashboard, Claude) is still running."""


@contextmanager
def _lock() -> Iterator[None]:
    """One sync per athlete at a time, across processes (POSIX only; on Windows syncs are not serialized)."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(DATA_DIR / ".sync.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncBusy("Another sync is already running; try again in a few minutes.") from exc
        yield
    finally:
        os.close(fd)


def _source() -> tuple[str, Any]:
    provider = settings.provider()
    if provider == "garmin":
        from . import garmin_sync
        return provider, garmin_sync
    if provider == "coros":
        from . import coros_sync
        return provider, coros_sync
    raise NotConnected("No data source connected yet: choose Garmin or COROS in the dashboard's Settings tab.")


def _auth_errors() -> tuple[type[Exception], ...]:
    from .coros_api import CorosAuthError
    from .garmin_api import GarminAuthError
    return CorosAuthError, GarminAuthError


def ensure_cache_matches(provider: str) -> None:
    """Data from another source must not mix with this one's (activity ids, health fields and limits differ)."""
    cached = store.get_meta("cache_provider")
    if cached != provider:
        if cached:
            _LOGGER.warning("data source changed %s -> %s: clearing the cache", cached, provider)
        store.clear_cache(provider)


def sync_all(days_back: int | None = None, max_fit_downloads: int | None = None) -> dict[str, Any]:
    with _lock():
        return _sync_all(days_back, max_fit_downloads)


def _sync_all(days_back: int | None, max_fit_downloads: int | None) -> dict[str, Any]:
    provider, source = _source()
    ensure_cache_matches(provider)
    try:
        fit = {} if max_fit_downloads is None else {"max_fit_downloads": max_fit_downloads}
        result = {"source": provider, "health": source.sync_health(days_back),
                  "activities": source.sync_activities(days_back, **fit)}
    except _auth_errors() as exc:
        store.set_meta("sync_error", str(exc))
        raise NotConnected(str(exc)) from exc
    if store.get_meta("recompute_needed"):
        from . import coach
        result["recomputed"] = coach.recompute(full=True)
        with store.connect() as conn:
            conn.execute("DELETE FROM meta WHERE key IN ('recompute_needed')")
    store.set_meta("last_sync", str(time.time()))
    with store.connect() as conn:
        conn.execute("DELETE FROM meta WHERE key = 'sync_error'")
    return result


def auto_sync() -> str | None:
    """Light incremental sync if the cache is stale. Returns a warning string on failure."""
    last = float(store.get_meta("last_sync") or 0)
    if time.time() - last < _AUTO_SYNC_INTERVAL_S:
        return None
    label = settings.provider_label()
    try:
        sync_all(max_fit_downloads=_AUTO_SYNC_FIT.get(settings.provider() or "", 10))
        return None
    except SyncBusy:
        return None  # someone else is refreshing the cache right now
    except NotConnected as exc:
        return f"{label} not connected, using cached data: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"{label} sync failed, using cached data: {str(exc)[:200]}"


# ---------------------------------------------------------------- background sync for the dashboard

_job: dict[str, Any] = {"running": False}
_job_lock = threading.Lock()


def start_background(days_back: int | None = None) -> dict[str, Any]:
    """Run sync_all in a thread (the dashboard polls job_status). One at a time."""
    with _job_lock:
        if _job["running"]:
            return dict(_job)
        _job.clear()
        _job.update(running=True, started=time.time())

    def run() -> None:
        try:
            result = sync_all(days_back)
            _job.update(ok=True, summary=_summarize(result))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("background sync failed")
            _job.update(ok=False, error=str(exc)[:300])
        finally:
            _job.update(running=False, finished=time.time())

    threading.Thread(target=run, daemon=True, name="sync").start()
    return dict(_job)


def job_status() -> dict[str, Any]:
    return dict(_job)


def _summarize(result: dict[str, Any]) -> str:
    acts = result.get("activities") or {}
    bits = [f"{acts.get('activities_seen', 0)} activities ({acts.get('new', 0)} new)",
            f"{acts.get('fit_analyzed', 0)} FIT files analyzed"]
    if acts.get("fit_pending_remaining"):
        bits.append(f"{acts['fit_pending_remaining']} FIT files still queued")
    failed = [k for k, v in (result.get("health") or {}).items() if isinstance(v, dict) and v.get("error")]
    if failed:
        bits.append("health errors: " + ", ".join(failed))
    return " · ".join(bits)
