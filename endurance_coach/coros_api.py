"""Client for the official COROS MCP server: OAuth (dynamic registration, PKCE login, refresh) and tool calls.

COROS answers with formatted text, not JSON; `parse.py` turns it into records.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

import requests

from .config import COROS_AUTH_FILE as AUTH_FILE
from .config import COROS_MCP_URL, LOGIN_PORT

_LOGGER = logging.getLogger(__name__)
_SCOPE = "openid mcp.tools offline_access"
_REFRESH_MARGIN_S = 24 * 3600
_PACE_S = 0.3
# COROS's guard answers malformed or out-of-range calls with this text instead of an error.
_GUARD = "Tool call anomalies detected"


class CorosError(RuntimeError):
    pass


class CorosAuthError(CorosError):
    """No valid login: connect COROS in the dashboard's Settings tab (or run `coach login`)."""


# ---------------------------------------------------------------- auth state

def _auth_server() -> str:
    parts = urllib.parse.urlparse(COROS_MCP_URL)
    return f"{parts.scheme}://{parts.netloc}"


def _load_auth() -> dict[str, Any]:
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save_auth(state: dict[str, Any]) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def _redirect_uri() -> str:
    # COROS accepts plain http only for loopback; the dashboard flow therefore asks for the address the browser lands on.
    return f"http://127.0.0.1:{LOGIN_PORT}/callback"


def _register(state: dict[str, Any]) -> dict[str, Any]:
    r = requests.post(f"{_auth_server()}/connect/register", json={
        "client_name": "endurance-coach (personal sync)",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "redirect_uris": [_redirect_uri()],
        "token_endpoint_auth_method": "none",
        "scope": _SCOPE,
    }, timeout=30)
    r.raise_for_status()
    state["client"] = r.json()
    _save_auth(state)
    return state


def _store_token(state: dict[str, Any], tok: dict[str, Any]) -> None:
    previous = (state.get("token") or {}).get("refresh_token")
    tok.setdefault("refresh_token", previous)
    tok["expires_at"] = time.time() + float(tok.get("expires_in") or 3600)
    tok.pop("id_token", None)
    state["token"] = tok
    _save_auth(state)


def begin_login() -> str:
    """Start a login: returns the COROS authorize URL. The PKCE verifier waits in the auth file for finish_login()."""
    state = _load_auth()
    if not state.get("client"):
        state = _register(state)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    nonce = secrets.token_urlsafe(16)
    state["pending"] = {"verifier": verifier, "state": nonce, "started": time.time()}
    _save_auth(state)
    return f"{_auth_server()}/oauth2/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": state["client"]["client_id"], "redirect_uri": _redirect_uri(),
        "scope": _SCOPE, "state": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": COROS_MCP_URL,
    })


def finish_login(callback: str | dict[str, str]) -> dict[str, Any]:
    """Exchange the code from the callback (the full URL the browser landed on, or its parsed query) for tokens."""
    if isinstance(callback, str):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(callback.strip()).query)
        callback = {k: v[0] for k, v in query.items()}
    state = _load_auth()
    pending = state.get("pending") or {}
    if not pending or time.time() - pending["started"] > 1800:
        raise CorosAuthError("No COROS login in progress (or it is older than 30 min). Start again.")
    if "code" not in callback:
        raise CorosAuthError(f"COROS login failed: {callback.get('error_description') or callback.get('error') or 'no code in the address'}")
    if callback.get("state") != pending["state"]:
        raise CorosAuthError("That address belongs to a different login attempt. Start again and paste the newest one.")
    r = requests.post(f"{_auth_server()}/oauth2/token", data={
        "grant_type": "authorization_code", "code": callback["code"], "redirect_uri": _redirect_uri(),
        "client_id": state["client"]["client_id"], "code_verifier": pending["verifier"], "resource": COROS_MCP_URL,
    }, timeout=30)
    if r.status_code in (400, 401):
        raise CorosAuthError(f"COROS refused the login code ({r.status_code}): it is single-use and expires quickly. Start again.")
    r.raise_for_status()
    state.pop("pending", None)
    _store_token(state, r.json())
    _session.reset()
    return {"saved_to": str(AUTH_FILE), "expires_in_days": round(float(state["token"].get("expires_in") or 0) / 86400)}


def login(open_browser: bool = True) -> dict[str, Any]:
    """Interactive login from the command line. The browser must reach 127.0.0.1:LOGIN_PORT on this machine
    (on a headless Pi: `ssh -L 8765:127.0.0.1:8765 pi`, or use the dashboard's Settings tab instead)."""
    url = begin_login()
    received: dict[str, str] = {}

    class _Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("COROS login received. You can close this tab.".encode())

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", LOGIN_PORT), _Callback)
    threading.Thread(target=server.handle_request, daemon=True).start()
    print(f"Log in to COROS in the browser:\n\n{url}\n", flush=True)
    if open_browser:
        webbrowser.open(url)
    deadline = time.time() + 600
    while not received and time.time() < deadline:
        time.sleep(0.5)
    server.server_close()
    if not received:
        raise CorosAuthError("Login timed out: no answer from the browser.")
    return finish_login(received)


def logout() -> None:
    AUTH_FILE.unlink(missing_ok=True)
    _session.reset()


def _refresh(state: dict[str, Any]) -> None:
    refresh_token = (state.get("token") or {}).get("refresh_token")
    if not refresh_token:
        raise CorosAuthError("Not logged in to COROS. Connect COROS in the dashboard's Settings tab.")
    r = requests.post(f"{_auth_server()}/oauth2/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": state["client"]["client_id"], "resource": COROS_MCP_URL,
    }, timeout=30)
    if r.status_code in (400, 401):
        raise CorosAuthError(f"COROS refresh token rejected ({r.status_code}). Connect COROS again in the dashboard's Settings tab.")
    r.raise_for_status()
    _store_token(state, r.json())


def access_token(force_refresh: bool = False) -> str:
    state = _load_auth()
    token = state.get("token") or {}
    if not token:
        raise CorosAuthError("Not logged in to COROS. Connect COROS in the dashboard's Settings tab.")
    if force_refresh or time.time() > float(token.get("expires_at") or 0) - _REFRESH_MARGIN_S:
        _refresh(state)
    return state["token"]["access_token"]


def auth_status() -> dict[str, Any]:
    token = _load_auth().get("token") or {}
    if not token:
        return {"logged_in": False}
    return {"logged_in": True, "access_token_expires": time.strftime("%Y-%m-%d %H:%M", time.localtime(token["expires_at"])),
            "has_refresh_token": bool(token.get("refresh_token"))}


# ---------------------------------------------------------------- MCP over streamable HTTP

class _Session:
    def __init__(self) -> None:
        self.http = requests.Session()
        self.http.headers.update({"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
        self.next_id = 0
        self.ready = False

    def _post(self, body: dict[str, Any]) -> requests.Response:
        self.http.headers["Authorization"] = f"Bearer {access_token()}"
        r = self.http.post(COROS_MCP_URL, json=body, timeout=180)
        if r.status_code == 401:
            self.http.headers["Authorization"] = f"Bearer {access_token(force_refresh=True)}"
            r = self.http.post(COROS_MCP_URL, json=body, timeout=180)
        if "Mcp-Session-Id" in r.headers:
            self.http.headers["Mcp-Session-Id"] = r.headers["Mcp-Session-Id"]
        return r

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        body = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        r = self._post(body)
        r.raise_for_status()
        if r.headers.get("Content-Type", "").startswith("text/event-stream"):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:])
                    if msg.get("id") == body["id"]:
                        return msg
            raise CorosError(f"No response to {method} in event stream")
        return r.json()

    def ensure_ready(self) -> None:
        if self.ready:
            return
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "endurance-coach", "version": "0.2"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.ready = True

    def reset(self) -> None:
        self.http.headers.pop("Mcp-Session-Id", None)
        self.ready = False


_session = _Session()


def call(tool: str, **arguments: Any) -> str:
    """Call a COROS tool and return its text. Retries on 429/5xx and re-initializes an expired session."""
    arguments = {k: v for k, v in arguments.items() if v is not None}
    for attempt in range(4):
        try:
            _session.ensure_ready()
            msg = _session.rpc("tools/call", {"name": tool, "arguments": arguments})
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in (400, 404) and attempt == 0:
                _session.reset()  # session expired on the server
                continue
            if status in (429, 500, 502, 503, 504) and attempt < 3:
                wait = 2 ** (attempt + 1) * (5 if status == 429 else 1)
                _LOGGER.warning("COROS HTTP %s, retrying in %ss", status, wait)
                time.sleep(wait)
                continue
            raise CorosError(f"COROS {tool}: HTTP {status}") from exc
        if "error" in msg:
            raise CorosError(f"COROS {tool}: {msg['error'].get('message', msg['error'])}")
        result = msg.get("result") or {}
        text = "\n".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
        if text.startswith('"') and text.endswith('"'):
            text = json.loads(text)  # some tools return a JSON-encoded string
        time.sleep(_PACE_S)
        if result.get("isError") or text.lstrip().startswith(_GUARD):
            raise CorosError(f"COROS {tool} refused {arguments}: {text[:300]}")
        return text
    raise CorosError(f"COROS {tool}: gave up after retries")


def list_tools() -> list[dict[str, Any]]:
    _session.ensure_ready()
    return _session.rpc("tools/list")["result"]["tools"]


def download(url: str) -> bytes:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content
