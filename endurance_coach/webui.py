"""Tiny web dashboard with a Settings tab (stdlib only). Bind it to the Tailscale address; there is no auth.

Writes (settings, keys, logins, sync, accepting a suggested plan change) only accept JSON POSTs from the page itself: a cross-site form can't send
application/json without a CORS preflight (never answered here), and the Origin header must match the host.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import dashboard, nutrition, settings, store, suggestions, sync, views
from .util import js_safe

_LOGGER = logging.getLogger(__name__)
_PAGE = Path(__file__).with_name("dashboard.html")
_MAX_BODY = 16_384


class _UnknownAction(Exception):
    pass


_SPORTS = ("swim", "bike", "run", "strength", "multisport")


def _view(path: str, query: dict[str, list[str]]) -> Any:
    """Read-only views the tabs load on demand (they take longer than the main dashboard payload)."""
    name = path.removeprefix("/api/view/")
    if name == "progress":
        return views.progress()
    if name == "recovery":
        return views.recovery()
    if name == "races":
        return views.races()
    if name == "nutrition":
        return nutrition.overview()
    if name == "sport":
        sport = (query.get("sport") or ["bike"])[0]
        if sport not in _SPORTS:
            raise ValueError(f"unknown sport {sport!r}")
        return views.sport_view(sport)
    if name.startswith("session/") and name.removeprefix("session/").isdigit():
        return views.session_detail(int(name.removeprefix("session/")))
    raise _UnknownAction(path)


def settings_state() -> dict[str, Any]:
    from . import coros_api, garmin_api

    raw_error = store.get_meta("sync_error")
    return {
        "app": settings.describe(),
        "providers": settings.PROVIDERS,
        "focuses": {k: v["label"] for k, v in settings.FOCUSES.items()},
        "garmin": garmin_api.auth_status(),
        "coros": coros_api.auth_status(),
        "cache_from": store.get_meta("cache_provider"),
        "last_sync": dashboard.last_sync(),
        "sync_error": raw_error,
        "sync_job": sync.job_status(),
    }


def _change_source(provider: str) -> dict[str, Any]:
    if provider not in settings.PROVIDERS:
        raise ValueError(f"unknown data source {provider!r}")
    if provider != settings.provider():
        settings.save(provider=provider)
    sync.ensure_cache_matches(provider)
    return {"started": sync.start_background()}


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    from . import coros_api, garmin_api

    if path == "/api/weight":
        return {"saved": nutrition.log_weight(body.get("weight_kg"), body.get("date"), body.get("note"))}
    if path == "/api/weight/delete":
        return nutrition.delete_weight(str(body.get("date") or ""))
    if path == "/api/suggestion":
        return suggestions.act(int(body.get("id") or 0), str(body.get("action") or ""))
    if path == "/api/settings/focus":
        settings.save(focus=body.get("focus"))
    elif path == "/api/settings/source":
        _change_source(str(body.get("provider")))
    elif path == "/api/garmin/key":
        garmin_api.save_key(str(body.get("key") or ""))
        if settings.provider() in (None, "garmin"):
            _change_source("garmin")
    elif path == "/api/garmin/forget":
        garmin_api.forget_key()
    elif path == "/api/coros/start":
        return {"url": coros_api.begin_login(), **settings_state()}
    elif path == "/api/coros/finish":
        coros_api.finish_login(str(body.get("callback") or ""))
        if settings.provider() in (None, "coros"):
            _change_source("coros")
    elif path == "/api/coros/logout":
        coros_api.logout()
    elif path == "/api/sync":
        sync.start_background()
    else:
        raise _UnknownAction(path)
    return settings_state()


class _Handler(BaseHTTPRequestHandler):
    server_version = "endurance-coach"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=str).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path, _, query = self.path.partition("?")
        try:
            if path in ("/", "/index.html"):
                self._send(200, _PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/dashboard":
                self._send(200, dashboard.build_json(), "application/json")
            elif path == "/api/settings":
                self._json(200, settings_state())
            elif path.startswith("/api/view/"):
                self._json(200, js_safe(_view(path, urllib.parse.parse_qs(query))))
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")
        except _UnknownAction:
            self._send(404, b"not found", "text/plain")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("dashboard request failed")
            self._json(500, {"error": str(exc)})

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # same-origin fetches from older browsers, and curl on the Pi
        return urllib.parse.urlparse(origin).netloc == self.headers.get("Host")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        if not self.headers.get("Content-Type", "").startswith("application/json") or not self._same_origin():
            self._json(403, {"error": "JSON requests from the dashboard page only"})
            return
        if length > _MAX_BODY:
            self._json(413, {"error": "request too large"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            self._json(200, _post(path, body if isinstance(body, dict) else {}))
        except _UnknownAction:
            self._json(404, {"error": f"unknown action {path}"})
        except Exception as exc:  # noqa: BLE001 - shown on the Settings tab
            _LOGGER.warning("settings action %s failed: %s", path, exc)
            self._json(400, {"error": str(exc)[:400], **settings_state()})

    def log_message(self, *args: object) -> None:
        pass


def serve(host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    suggestions.resume()
    print(f"Dashboard on http://{host}:{port}", flush=True)
    httpd.serve_forever()
