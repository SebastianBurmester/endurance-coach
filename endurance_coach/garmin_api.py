"""Thin wrapper over Garmin's official Personal API client: per-athlete key, token refresh, retries, pacing.

The key lives in this athlete's data/auth/, never in the shared ~/.config/garmin: several athletes can share one
machine, and a shared key would sync the wrong person's account."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from garmin_personal_api import (
    GarminPersonalApiClient,
    HttpError,
    PersonalApiAuthenticator,
    resolve_api_key,
)

from .config import GARMIN_KEY_FILE, GARMIN_TOKEN_FILE

_LOGGER = logging.getLogger(__name__)
_REFRESH_MARGIN_S = 3600
_PACE_S = 0.3

_client: GarminPersonalApiClient | None = None
_expires_at = 0.0


class GarminAuthError(RuntimeError):
    """No usable Personal API key: add one in the dashboard's Settings tab."""


def _key() -> str:
    try:
        return resolve_api_key(key_file=GARMIN_KEY_FILE)
    except Exception as exc:  # noqa: BLE001 - the library raises its own types for missing/empty keys
        raise GarminAuthError("No Garmin Personal API key. Add it in the dashboard's Settings tab.") from exc


def _authenticate(force: bool = False) -> GarminPersonalApiClient:
    global _client, _expires_at
    try:
        with PersonalApiAuthenticator(bearer_token_cache_file=GARMIN_TOKEN_FILE) as auth:
            token = auth.obtain_token(_key(), force_refresh=force)
    except HttpError as exc:
        if exc.status_code in (400, 401, 403):
            raise GarminAuthError(f"Garmin rejected the Personal API key (HTTP {exc.status_code}). "
                                  "Create a new one and add it in the Settings tab.") from exc
        raise
    if _client is None:
        _client = GarminPersonalApiClient(token.access_token, timeout=60)
    else:
        _client.update_bearer_token(token.access_token)
    _expires_at = token.expires_at
    return _client


def client() -> GarminPersonalApiClient:
    if _client is None or time.time() > _expires_at - _REFRESH_MARGIN_S:
        return _authenticate()
    return _client


def call(method: str | Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a client method by name, refreshing the token on 401 and backing off on 429/5xx."""
    reauthed = False
    for attempt in range(4):
        c = client()
        fn = getattr(c, method) if isinstance(method, str) else method
        try:
            result = fn(*args, **kwargs)
            time.sleep(_PACE_S)
            return result
        except HttpError as exc:
            if exc.status_code == 401 and not reauthed:
                _authenticate(force=True)
                reauthed = True
                continue
            if exc.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                wait = 2 ** (attempt + 1) * (5 if exc.status_code == 429 else 1)
                _LOGGER.warning("HTTP %s, retrying in %ss", exc.status_code, wait)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def save_key(key: str) -> None:
    """Store this athlete's key (mode 0600) and check it by exchanging it for a bearer token."""
    global _client, _expires_at
    key = key.strip()
    if not key:
        raise GarminAuthError("The key is empty.")
    GARMIN_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    previous = GARMIN_KEY_FILE.read_text(encoding="utf-8") if GARMIN_KEY_FILE.exists() else None
    descriptor = os.open(GARMIN_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
        fh.write(key)
    _client, _expires_at = None, 0.0
    try:
        _authenticate(force=True)
    except Exception:
        # Keep the previous working key rather than a rejected one.
        if previous is None:
            GARMIN_KEY_FILE.unlink(missing_ok=True)
        else:
            GARMIN_KEY_FILE.write_text(previous, encoding="utf-8")
        _client, _expires_at = None, 0.0
        raise


def forget_key() -> None:
    global _client, _expires_at
    GARMIN_KEY_FILE.unlink(missing_ok=True)
    GARMIN_TOKEN_FILE.unlink(missing_ok=True)
    _client, _expires_at = None, 0.0


def auth_status() -> dict[str, Any]:
    if not GARMIN_KEY_FILE.exists():
        return {"connected": False}
    return {"connected": True, "key": "the saved Personal API key",
            "token_expires": time.strftime("%Y-%m-%d %H:%M", time.localtime(_expires_at)) if _expires_at else None}
