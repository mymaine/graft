"""Helper loader: validate, circuit-check via daemon, import, auto-wrap stats."""

from __future__ import annotations

import base64
import contextlib
import contextvars
import functools
import importlib.util
import inspect
import json as _json
import os
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import httpx

from graft import git_memory, validator

PORT_FILE = Path(".graft/daemon.port")

POSITIVE_TEMPLATE = '''def list_issues(owner: str, repo: str, state: str = "open") -> list[dict]:
    """List GitHub issues for a repository.

    Generalization:
        Works for any (owner, repo). Filter by state, limit count.
        Variant example: list_issues("python", "cpython", state="closed")
        Not applicable: GitHub Enterprise on custom domains.
    """
    return context.request(
        "github",
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        params={"state": state},
    ).json()
'''

# ContextVar propagates across asyncio tasks but not threading.Thread.
# v1 helpers should not spawn threads/processes that re-enter wrapped helpers.
_in_helper: contextvars.ContextVar[bool] = contextvars.ContextVar("graft_in_helper", default=False)


@dataclass(frozen=True)
class Response:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return _json.loads(self.body)

    def text(self) -> str:
        return self.body.decode("utf-8")


class _HelperError(Exception):
    error: ClassVar[str] = ""
    source: ClassVar[str] = "daemon"

    def __init__(self, *, service: str, reason: str, **extra: Any) -> None:
        self.service = service
        self.reason = reason
        self.extra = extra
        super().__init__(f"{self.error}: {service}: {reason}")


class HelperLoadError(_HelperError):
    error: ClassVar[str] = "HelperLoadError"


class HelperImportError(_HelperError):
    error: ClassVar[str] = "HelperImportError"


# Names without the Error suffix are dictated by the spec error schema
# (docs/spec.md "錯誤類型 schema"); ruff's N818 default does not apply here.
class HelperLoadAborted(_HelperError):  # noqa: N818
    error: ClassVar[str] = "HelperLoadAborted"


class DaemonNotRunning(_HelperError):  # noqa: N818
    error: ClassVar[str] = "DaemonNotRunning"
    source: ClassVar[str] = "client"


def _connect(port_file: Path) -> httpx.Client:
    if not port_file.exists():
        raise DaemonNotRunning(service="-", reason=f"port file missing: {port_file}")
    pid_str, _, port_str = port_file.read_text(encoding="utf-8").strip().partition(":")
    try:
        pid, port = int(pid_str), int(port_str)
        os.kill(pid, 0)
    except ValueError as e:
        raise DaemonNotRunning(service="-", reason="daemon.port malformed") from e
    except (ProcessLookupError, OSError):
        port_file.unlink(missing_ok=True)
        raise DaemonNotRunning(service="-", reason=f"daemon dead (pid={pid})") from None
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0, trust_env=False)


def load(
    service: str,
    helpers_dir: Path = Path("helpers"),
    port_file: Path = PORT_FILE,
) -> ModuleType:
    src = helpers_dir / f"{service}.py"
    if not src.exists():
        raise HelperImportError(service=service, reason=f"helper file not found: {src}")
    fail = validator.check(src.read_text(encoding="utf-8"), service)
    with _connect(port_file) as client:
        try:
            r = client.post("/circuit/check", json={"service": service, "ok": fail is None})
            r.raise_for_status()
            action = r.json()["action"]
        except httpx.HTTPStatusError as e:
            raise HelperImportError(
                service=service,
                reason=f"daemon error: {e.response.status_code}",
            ) from e
    if fail:
        if action == "abort":
            raise HelperLoadAborted(
                service=service, reason=fail, reset_hint=f"graft reset {service}"
            )
        kw = {"template": POSITIVE_TEMPLATE} if action == "raise_with_template" else {}
        raise HelperLoadError(service=service, reason=fail, **kw)
    name = f"helpers.{service}"
    spec = importlib.util.spec_from_file_location(name, src)
    if spec is None or spec.loader is None:
        raise HelperImportError(service=service, reason=f"cannot build spec for {src}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(name, None)
        raise HelperImportError(service=service, reason=str(e)) from e
    public: list[str] = []
    for fn_name, fn in inspect.getmembers(module, inspect.isfunction):
        if not fn_name.startswith("_") and fn.__module__ == name:
            setattr(module, fn_name, _track(fn, service, port_file))
            public.append(fn_name)
    if git_memory.should_commit(cwd=helpers_dir.parent)[0]:
        msg = f"graft: load {service} ({', '.join(public)})"
        git_memory.commit_helpers(msg, cwd=helpers_dir.parent)
    return module


def _track(fn: Callable[..., Any], service: str, port_file: Path) -> Callable[..., Any]:
    @functools.wraps(fn)
    def w(*args: Any, **kwargs: Any) -> Any:
        if _in_helper.get():
            return fn(*args, **kwargs)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ok = True
        token = _in_helper.set(True)
        try:
            return fn(*args, **kwargs)
        except Exception:
            ok = False
            raise
        finally:
            _in_helper.reset(token)
            payload = {"service": service, "helper": fn.__name__, "ts": ts, "ok": ok}
            with contextlib.suppress(Exception), _connect(port_file) as c:
                c.post("/stats", json=payload)

    return w


def request(
    service: str,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json: Any = None,
    timeout: float | None = None,
    port_file: Path = PORT_FILE,
) -> Response:
    payload: dict[str, Any] = {"service": service, "method": method, "url": url}
    for k, v in (("params", params), ("headers", headers), ("json", json), ("timeout", timeout)):
        if v is not None:
            payload[k] = v
    with _connect(port_file) as c:
        d = c.post("/request", json=payload).json()
    return Response(d["status"], d["headers"], base64.b64decode(d["body_b64"]))


def auth(service: str, auth_path: Path = Path(".graft/auth.toml")) -> str | None:
    if env := os.environ.get(f"GRAFT_{re.sub(r'[^A-Z0-9]', '_', service.upper())}_TOKEN"):
        return env
    if not auth_path.exists():
        return None
    try:
        data = tomllib.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    sec = data.get(service)
    return tok if isinstance(sec, dict) and isinstance(tok := sec.get("token"), str) else None
