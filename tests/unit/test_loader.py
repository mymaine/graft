"""Unit tests for graft.loader + graft.context.

Strategy:
  - Mock daemon fixture: real ThreadingHTTPServer in a thread serving 5 routes
    (/circuit/check, /stats, /request, /health, /reload). Captures every POST
    body for test assertions. Mirrors the pattern used in test_daemon.py so a
    future shared conftest can absorb both.
  - Helper files are written under tmp_path / "helpers" and the directory is
    added to sys.path for the duration of the test (fixture cleans up).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from graft import context, loader
from graft.loader import (
    DaemonNotRunning,
    HelperImportError,
    HelperLoadAborted,
    HelperLoadError,
    Response,
)

# ---------------------------------------------------------------------------
# Mock daemon fixture
# ---------------------------------------------------------------------------


class _MockDaemon:
    """Captures requests and serves scripted responses for the 5 routes."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.circuit_responses: list[dict[str, Any]] = []
        self.request_response: dict[str, Any] = {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body_b64": base64.b64encode(b'{"ok":true}').decode("ascii"),
            "encoding": "utf8",
        }
        self.stats_status: int = 200
        self.lock = threading.Lock()

    def next_circuit_response(self) -> dict[str, Any]:
        with self.lock:
            if self.circuit_responses:
                return self.circuit_responses.pop(0)
        return {"count": 0, "action": "ok"}


def _make_handler(mock: _MockDaemon) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # silence stderr
            return

        def do_GET(self) -> None:
            self._reply(200, {"ok": True, "pid": os.getpid(), "port": 0, "version": "test"})

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else {}
            with mock.lock:
                mock.requests.append((self.path, body))
            if self.path == "/circuit/check":
                self._reply(200, mock.next_circuit_response())
            elif self.path == "/stats":
                self._reply(mock.stats_status, {"ok": mock.stats_status == 200})
            elif self.path == "/request":
                self._reply(200, mock.request_response)
            else:
                self._reply(404, {"error": "NotFound"})

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


@pytest.fixture
def mock_daemon(tmp_path: Path) -> Iterator[tuple[_MockDaemon, Path]]:
    mock = _MockDaemon()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(mock))
    port = server.server_address[1]
    port_file = tmp_path / "daemon.port"
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(f"{os.getpid()}:{port}", encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not port_file.exists():
        time.sleep(0.01)
    try:
        yield mock, port_file
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.fixture
def helpers_dir(tmp_path: Path) -> Iterator[Path]:
    d = tmp_path / "helpers"
    d.mkdir()
    yield d
    # Drop loaded helper modules so subsequent tests get fresh state.
    for name in list(sys.modules):
        if name.startswith("helpers."):
            del sys.modules[name]


VALID_HELPER = '''
def list_issues(owner: str, repo: str) -> dict:
    """List GitHub issues.

    Generalization:
        Works for any (owner, repo).
    """
    return {"owner": owner, "repo": repo}
'''


# ---------------------------------------------------------------------------
# load() — happy path & wrap behaviour
# ---------------------------------------------------------------------------


def test_load_happy_path_wraps_public_function(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    mock, port_file = mock_daemon
    (helpers_dir / "github.py").write_text(VALID_HELPER, encoding="utf-8")

    module = loader.load("github", helpers_dir=helpers_dir, port_file=port_file)

    result = module.list_issues("anthropics", "claude-code")
    assert result == {"owner": "anthropics", "repo": "claude-code"}
    paths = [p for p, _ in mock.requests]
    assert paths.count("/circuit/check") == 1
    assert paths.count("/stats") == 1
    stats_body = next(b for p, b in mock.requests if p == "/stats")
    assert stats_body["service"] == "github"
    assert stats_body["helper"] == "list_issues"
    assert stats_body["ok"] is True


def test_load_missing_file_raises_helper_import_error(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    _, port_file = mock_daemon
    with pytest.raises(HelperImportError) as exc:
        loader.load("ghost", helpers_dir=helpers_dir, port_file=port_file)
    assert "not found" in exc.value.reason


def test_load_validator_failure_count_one_raises_helper_load_error(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    mock, port_file = mock_daemon
    mock.circuit_responses.append({"count": 1, "action": "raise"})
    (helpers_dir / "github.py").write_text(
        'def list_issues(owner: str) -> None:\n    """no generalization here."""\n',
        encoding="utf-8",
    )

    with pytest.raises(HelperLoadError) as exc:
        loader.load("github", helpers_dir=helpers_dir, port_file=port_file)
    assert "Generalization" in exc.value.reason
    assert "template" not in exc.value.extra


def test_load_validator_failure_count_three_includes_template(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    mock, port_file = mock_daemon
    mock.circuit_responses.append({"count": 3, "action": "raise_with_template"})
    (helpers_dir / "github.py").write_text(
        'def list_issues(owner: str) -> None:\n    """no generalization here."""\n',
        encoding="utf-8",
    )

    with pytest.raises(HelperLoadError) as exc:
        loader.load("github", helpers_dir=helpers_dir, port_file=port_file)
    assert "Generalization:" in exc.value.extra["template"]


def test_load_validator_failure_count_five_aborts(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    mock, port_file = mock_daemon
    mock.circuit_responses.append({"count": 5, "action": "abort"})
    (helpers_dir / "github.py").write_text(
        'def list_issues(owner: str) -> None:\n    """no generalization here."""\n',
        encoding="utf-8",
    )

    with pytest.raises(HelperLoadAborted) as exc:
        loader.load("github", helpers_dir=helpers_dir, port_file=port_file)
    assert exc.value.extra["reset_hint"] == "graft reset github"


def test_load_cross_service_import_rejected(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    mock, port_file = mock_daemon
    mock.circuit_responses.append({"count": 1, "action": "raise"})
    (helpers_dir / "notion.py").write_text(
        "from helpers.github import list_issues\n", encoding="utf-8"
    )

    with pytest.raises(HelperLoadError) as exc:
        loader.load("notion", helpers_dir=helpers_dir, port_file=port_file)
    assert "forbidden cross-service import" in exc.value.reason


def test_load_syntax_error_reported(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    mock, port_file = mock_daemon
    mock.circuit_responses.append({"count": 1, "action": "raise"})
    (helpers_dir / "github.py").write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(HelperLoadError) as exc:
        loader.load("github", helpers_dir=helpers_dir, port_file=port_file)
    assert "syntax error" in exc.value.reason


def test_load_import_error_after_validator_passes(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    _, port_file = mock_daemon
    (helpers_dir / "github.py").write_text(
        "import not_a_real_module_xyz\n\n"
        "def list_issues(owner: str) -> None:\n"
        '    """List.\n\n    Generalization:\n        Any owner.\n    """\n',
        encoding="utf-8",
    )

    with pytest.raises(HelperImportError):
        loader.load("github", helpers_dir=helpers_dir, port_file=port_file)


def test_wrap_does_not_double_count_internal_calls(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    mock, port_file = mock_daemon
    (helpers_dir / "github.py").write_text(
        '''
def foo(x: int) -> int:
    """Inner.

    Generalization:
        Any int.
    """
    return x + 1


def bar(x: int) -> int:
    """Outer.

    Generalization:
        Any int.
    """
    return foo(x) * 2
''',
        encoding="utf-8",
    )
    module = loader.load("github", helpers_dir=helpers_dir, port_file=port_file)

    assert module.bar(2) == 6
    stats_records = [b for p, b in mock.requests if p == "/stats"]
    assert len(stats_records) == 1
    assert stats_records[0]["helper"] == "bar"


def test_wrap_records_failure_when_helper_raises(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    mock, port_file = mock_daemon
    (helpers_dir / "github.py").write_text(
        '''
def boom() -> None:
    """Crash.

    Generalization:
        N/A.
    """
    raise ValueError("nope")
''',
        encoding="utf-8",
    )
    module = loader.load("github", helpers_dir=helpers_dir, port_file=port_file)

    with pytest.raises(ValueError, match="nope"):
        module.boom()
    stats_records = [b for p, b in mock.requests if p == "/stats"]
    assert len(stats_records) == 1
    assert stats_records[0]["ok"] is False


def test_wrap_stats_failure_does_not_break_helper(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    mock, port_file = mock_daemon
    (helpers_dir / "github.py").write_text(VALID_HELPER, encoding="utf-8")
    module = loader.load("github", helpers_dir=helpers_dir, port_file=port_file)

    mock.stats_status = 500
    assert module.list_issues("a", "b") == {"owner": "a", "repo": "b"}


def test_helper_class_is_not_wrapped(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    """A class defined in the helper module must stay as a class, not get wrapped."""
    mock, port_file = mock_daemon
    (helpers_dir / "github.py").write_text(
        '''
class Foo:
    """A bare class. Construction must not produce a stats record."""

    def __init__(self) -> None:
        self.value = 42


def bar() -> int:
    """Public helper.

    Generalization:
        Any context.
    """
    return 1
''',
        encoding="utf-8",
    )
    module = loader.load("github", helpers_dir=helpers_dir, port_file=port_file)

    assert isinstance(module.Foo, type)
    instance = module.Foo()
    assert instance.value == 42
    assert module.bar() == 1

    stats_records = [b for p, b in mock.requests if p == "/stats"]
    assert len(stats_records) == 1
    assert stats_records[0]["helper"] == "bar"


def test_imported_stdlib_function_is_not_wrapped(
    mock_daemon: tuple[_MockDaemon, Path], helpers_dir: Path
) -> None:
    """Re-imported stdlib symbols (different __module__) must not be wrapped."""
    mock, port_file = mock_daemon
    (helpers_dir / "github.py").write_text(
        '''from datetime import datetime


def public_fn() -> int:
    """Public.

    Generalization:
        Any context.
    """
    return 7
''',
        encoding="utf-8",
    )
    module = loader.load("github", helpers_dir=helpers_dir, port_file=port_file)

    from datetime import datetime as real_datetime

    assert module.datetime is real_datetime
    assert module.public_fn() == 7

    stats_records = [b for p, b in mock.requests if p == "/stats"]
    assert len(stats_records) == 1
    assert stats_records[0]["helper"] == "public_fn"


# ---------------------------------------------------------------------------
# DaemonNotRunning paths
# ---------------------------------------------------------------------------


def test_load_raises_daemon_not_running_when_port_file_missing(
    helpers_dir: Path, tmp_path: Path
) -> None:
    (helpers_dir / "github.py").write_text(VALID_HELPER, encoding="utf-8")
    port_file = tmp_path / "daemon.port"

    with pytest.raises(DaemonNotRunning) as exc:
        loader.load("github", helpers_dir=helpers_dir, port_file=port_file)
    assert exc.value.source == "client"


def test_load_raises_daemon_not_running_when_pid_dead(helpers_dir: Path, tmp_path: Path) -> None:
    (helpers_dir / "github.py").write_text(VALID_HELPER, encoding="utf-8")
    port_file = tmp_path / "daemon.port"
    port_file.write_text("99999999:54321", encoding="utf-8")

    with pytest.raises(DaemonNotRunning):
        loader.load("github", helpers_dir=helpers_dir, port_file=port_file)
    assert not port_file.exists()


def test_load_raises_daemon_not_running_when_port_file_malformed(
    helpers_dir: Path, tmp_path: Path
) -> None:
    (helpers_dir / "github.py").write_text(VALID_HELPER, encoding="utf-8")
    port_file = tmp_path / "daemon.port"
    port_file.write_text("garbage", encoding="utf-8")

    with pytest.raises(DaemonNotRunning):
        loader.load("github", helpers_dir=helpers_dir, port_file=port_file)


# ---------------------------------------------------------------------------
# request() public API
# ---------------------------------------------------------------------------


def test_request_returns_response_with_json(mock_daemon: tuple[_MockDaemon, Path]) -> None:
    mock, port_file = mock_daemon
    mock.request_response = {
        "status": 201,
        "headers": {"content-type": "application/json"},
        "body_b64": base64.b64encode(b'{"id":7}').decode("ascii"),
        "encoding": "utf8",
    }

    resp = loader.request("github", "GET", "https://api.github.com/x", port_file=port_file)
    assert isinstance(resp, Response)
    assert resp.status_code == 201
    assert resp.json() == {"id": 7}
    assert resp.text() == '{"id":7}'

    body = next(b for p, b in mock.requests if p == "/request")
    assert body["service"] == "github"
    assert body["method"] == "GET"
    assert body["url"] == "https://api.github.com/x"


def test_request_forwards_optional_kwargs(mock_daemon: tuple[_MockDaemon, Path]) -> None:
    mock, port_file = mock_daemon
    loader.request(
        "github",
        "POST",
        "https://api.github.com/x",
        params={"state": "open"},
        headers={"X-Test": "1"},
        json={"a": 1},
        timeout=5.0,
        port_file=port_file,
    )
    body = next(b for p, b in mock.requests if p == "/request")
    assert body["params"] == {"state": "open"}
    assert body["headers"] == {"X-Test": "1"}
    assert body["json"] == {"a": 1}
    assert body["timeout"] == 5.0


# ---------------------------------------------------------------------------
# auth() public API
# ---------------------------------------------------------------------------


def test_auth_env_beats_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAFT_FOO_TOKEN", "env_val")
    auth_path = tmp_path / "auth.toml"
    auth_path.write_text('[foo]\ntoken = "toml_val"\n', encoding="utf-8")

    assert loader.auth("foo", auth_path=auth_path) == "env_val"


def test_auth_falls_back_to_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAFT_FOO_TOKEN", raising=False)
    auth_path = tmp_path / "auth.toml"
    auth_path.write_text('[foo]\ntoken = "toml_val"\n', encoding="utf-8")

    assert loader.auth("foo", auth_path=auth_path) == "toml_val"


def test_auth_normalizes_service_name_for_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRAFT_GOOGLE_DRIVE_TOKEN", "abc")
    assert loader.auth("google-drive", auth_path=tmp_path / "auth.toml") == "abc"


def test_auth_returns_none_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GRAFT_NOPE_TOKEN", raising=False)
    assert loader.auth("nope", auth_path=tmp_path / "missing.toml") is None


def test_auth_returns_none_for_malformed_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GRAFT_FOO_TOKEN", raising=False)
    auth_path = tmp_path / "auth.toml"
    auth_path.write_text("not = [valid", encoding="utf-8")
    assert loader.auth("foo", auth_path=auth_path) is None


# ---------------------------------------------------------------------------
# context namespace facade
# ---------------------------------------------------------------------------


def test_context_reexports_public_api() -> None:
    assert context.request is loader.request
    assert context.auth is loader.auth
    assert context.Response is loader.Response


def test_response_dataclass_basics() -> None:
    r = Response(status_code=200, headers={"content-type": "application/json"}, body=b'{"a":1}')
    assert r.json() == {"a": 1}
    assert r.text() == '{"a":1}'
