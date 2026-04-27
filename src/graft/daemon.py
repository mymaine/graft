"""HTTP localhost daemon: 5 routes, auth injection, retry, base64 body relay.

Stdlib http.server keeps deps thin; ThreadingHTTPServer accepts the spec-known
race that circuit counts may differ by 1 under concurrent /circuit/check.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import json
import os
import re
import sys
import tomllib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import httpx

from graft import stats
from graft.circuit import Circuit

VERSION = version("graft")
DEFAULT_TIMEOUT = 30.0
TEXTY = ("json", "text", "xml", "javascript")
_ENV_KEY_SAFE = re.compile(r"[^A-Z0-9]")


class Daemon:
    """Holds server state: circuit, config paths, httpx transport. The handler is wire-only."""

    def __init__(
        self,
        auth_path: Path,
        stats_path: Path = stats.DEFAULT_PATH,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.auth_path = auth_path
        self.stats_path = stats_path
        self.circuit = Circuit()
        self._transport = transport or httpx.HTTPTransport(retries=2)
        self._server: ThreadingHTTPServer | None = None
        self.actual_port: int = 0
        self._auth_cache: dict[str, Any] | None = None

    def bind(self, host: str, port: int, port_file: Path) -> None:
        srv = ThreadingHTTPServer((host, port), _Handler)
        srv.daemon_ref = self  # type: ignore[attr-defined]
        self._server = srv
        self.actual_port = srv.server_address[1]
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(f"{os.getpid()}:{self.actual_port}", encoding="utf-8")
        atexit.register(_unlink_quiet, port_file)

    def _handle_request(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = dict(body.get("headers") or {})
        token = self._lookup_token(str(body["service"]))
        if token and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {token}"
        timeout = float(tv) if (tv := body.get("timeout")) is not None else DEFAULT_TIMEOUT
        with httpx.Client(transport=self._transport, timeout=timeout) as c:
            r = c.request(
                str(body["method"]),
                str(body["url"]),
                headers=headers,
                params=body.get("params"),
                json=body.get("json"),
            )
        ct = r.headers.get("content-type", "").lower()
        return {
            "status": r.status_code,
            "headers": dict(r.headers),
            "body_b64": base64.b64encode(r.content).decode("ascii"),
            "encoding": "utf8" if any(h in ct for h in TEXTY) else "binary",
        }

    def _handle_stats(self, body: dict[str, Any]) -> dict[str, Any]:
        stats.append(
            str(body["service"]),
            str(body["helper"]),
            datetime.fromisoformat(str(body["ts"])),
            bool(body["ok"]),
            path=self.stats_path,
        )
        return {"ok": True}

    def _handle_circuit(self, body: dict[str, Any]) -> dict[str, Any]:
        if bool(body["ok"]):
            self.circuit.record_success(str(body["service"]))
            return {"count": 0, "action": "ok"}
        a = self.circuit.record_failure(str(body["service"]))
        return {"count": a.count, "action": a.action}

    def _handle_circuit_reset(self, body: dict[str, Any]) -> dict[str, Any]:
        self.circuit.reset(str(body["service"]))
        return {"ok": True}

    def _handle_health(self, _body: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": True, "pid": os.getpid(), "port": self.actual_port, "version": VERSION}

    def _handle_reload(self, _body: dict[str, Any]) -> dict[str, Any]:
        return {"loaded": [], "errors": []}

    def _lookup_token(self, service: str) -> str | None:
        """Env var beats auth.toml; auth.toml is read once on first lookup, restart to reload."""
        if env := os.environ.get(f"GRAFT_{_ENV_KEY_SAFE.sub('_', service.upper())}_TOKEN"):
            return env
        if self._auth_cache is None:
            self._auth_cache = _read_toml(self.auth_path)
        sec = self._auth_cache.get(service)
        return tok if isinstance(sec, dict) and isinstance(tok := sec.get("token"), str) else None


def serve(
    host: str = "127.0.0.1",
    port: int = 0,
    port_file: Path = Path(".graft/daemon.port"),
    auth_path: Path = Path(".graft/auth.toml"),
) -> None:
    """Start daemon, write pid:port file, block until shutdown."""
    if port_file.exists():
        existing = port_file.read_text(encoding="utf-8").strip()
        with contextlib.suppress(ValueError, ProcessLookupError, OSError):
            os.kill(int(existing.partition(":")[0]), 0)
            print(f"daemon already running ({existing})", file=sys.stderr)
            sys.exit(1)
    d = Daemon(auth_path=auth_path)
    d.bind(host=host, port=port, port_file=port_file)
    cast("ThreadingHTTPServer", d._server).serve_forever()


def _unlink_quiet(p: Path) -> None:
    with contextlib.suppress(OSError):
        p.unlink(missing_ok=True)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"warning: daemon: auth.toml unreadable: {e}", file=sys.stderr)
        return {}


_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/health"): "_handle_health",
    ("POST", "/request"): "_handle_request",
    ("POST", "/stats"): "_handle_stats",
    ("POST", "/circuit/check"): "_handle_circuit",
    ("POST", "/circuit/reset"): "_handle_circuit_reset",
    ("POST", "/reload"): "_handle_reload",
}


class _Handler(BaseHTTPRequestHandler):
    """Wire-only HTTP layer; dispatches to Daemon._handle_*."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if (name := _ROUTES.get((method, self.path))) is None:
            self._reply(404, {"error": "NotFound", "source": "daemon", "reason": self.path})
            return
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        d = cast("Daemon", self.server.daemon_ref)  # type: ignore[attr-defined]
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
            result = getattr(d, name)(body)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            self._reply(400, {"error": "BadRequest", "source": "daemon", "reason": str(e)})
            return
        except Exception as e:
            print(f"daemon error in {method} {self.path}: {type(e).__name__}: {e}", file=sys.stderr)
            self._reply(
                500, {"error": "InternalError", "source": "daemon", "reason": type(e).__name__}
            )
            return
        self._reply(200, result)

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
