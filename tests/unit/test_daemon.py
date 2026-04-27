"""Unit tests for graft.daemon.

Two layers:
  - Pure handler-method tests (no socket): exercise auth injection, /stats wire,
    /circuit wire, /reload stub, /request encoding choice via httpx.MockTransport.
  - One thread-fixture smoke test: real ThreadingHTTPServer behind a thread,
    hit via httpx — covers wire (status, JSON shape, /health, port-file).

The split keeps the suite fast while still proving the HTTP layer works.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from graft.daemon import Daemon, serve

FIXED_ISO = "2026-04-26T15:30:21+00:00"

Handler = Callable[[httpx.Request], httpx.Response]


def _mock_transport(handler: Handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Pure handler-method tests — no socket
# ---------------------------------------------------------------------------


def test_handle_stats_appends_to_jsonl(tmp_path: Path) -> None:
    stats_path = tmp_path / "stats.jsonl"
    d = Daemon(auth_path=tmp_path / "auth.toml", stats_path=stats_path)

    body = {"service": "github", "helper": "list_issues", "ts": FIXED_ISO, "ok": True}
    result = d._handle_stats(body)

    assert result == {"ok": True}
    lines = stats_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["service"] == "github"
    assert record["helper"] == "list_issues"
    assert record["ok"] is True


def test_handle_circuit_failure_progression() -> None:
    d = Daemon(auth_path=Path("/dev/null"), stats_path=Path("/dev/null"))

    actions = [d._handle_circuit({"service": "github", "ok": False}) for _ in range(5)]

    assert actions[0] == {"count": 1, "action": "raise"}
    assert actions[2] == {"count": 3, "action": "raise_with_template"}
    assert actions[4] == {"count": 5, "action": "abort"}


def test_handle_circuit_success_resets() -> None:
    d = Daemon(auth_path=Path("/dev/null"), stats_path=Path("/dev/null"))
    for _ in range(3):
        d._handle_circuit({"service": "github", "ok": False})

    ok_result = d._handle_circuit({"service": "github", "ok": True})
    next_fail = d._handle_circuit({"service": "github", "ok": False})

    assert ok_result == {"count": 0, "action": "ok"}
    assert next_fail == {"count": 1, "action": "raise"}


def test_handle_circuit_reset_clears_counter() -> None:
    d = Daemon(auth_path=Path("/dev/null"), stats_path=Path("/dev/null"))
    for _ in range(4):
        d._handle_circuit({"service": "github", "ok": False})

    result = d._handle_circuit_reset({"service": "github"})

    assert result == {"ok": True}
    after = d._handle_circuit({"service": "github", "ok": False})
    assert after == {"count": 1, "action": "raise"}


def test_handle_request_returns_base64_utf8_for_json(tmp_path: Path) -> None:
    seen: dict[str, httpx.Request] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"x":1}')

    d = Daemon(
        auth_path=tmp_path / "auth.toml",
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )

    out = d._handle_request(
        {
            "service": "github",
            "method": "GET",
            "url": "https://api.github.com/x",
        }
    )

    assert out["status"] == 200
    assert out["encoding"] == "utf8"
    assert base64.b64decode(out["body_b64"]) == b'{"x":1}'
    assert "authorization" not in {k.lower() for k in seen["req"].headers}


def test_handle_request_auth_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAFT_GITHUB_TOKEN", "abc")
    seen: dict[str, str | None] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    d = Daemon(
        auth_path=tmp_path / "auth.toml",
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )
    d._handle_request({"service": "github", "method": "GET", "url": "https://api.github.com/x"})

    assert seen["auth"] == "Bearer abc"


def test_handle_request_auth_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAFT_GITHUB_TOKEN", raising=False)
    auth = tmp_path / "auth.toml"
    auth.write_text('[github]\ntoken = "xyz"\n', encoding="utf-8")

    seen: dict[str, str | None] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    d = Daemon(
        auth_path=auth,
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )
    d._handle_request({"service": "github", "method": "GET", "url": "https://api.github.com/x"})

    assert seen["auth"] == "Bearer xyz"


def test_handle_request_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAFT_GITHUB_TOKEN", "from-env")
    auth = tmp_path / "auth.toml"
    auth.write_text('[github]\ntoken = "from-toml"\n', encoding="utf-8")

    seen: dict[str, str | None] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    d = Daemon(
        auth_path=auth,
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )
    d._handle_request({"service": "github", "method": "GET", "url": "https://api.github.com/x"})

    assert seen["auth"] == "Bearer from-env"


def test_handle_request_no_auth_when_neither_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GRAFT_NOAUTH_TOKEN", raising=False)
    seen: dict[str, list[str]] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["headers"] = [k.lower() for k in request.headers]
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    d = Daemon(
        auth_path=tmp_path / "auth.toml",
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )
    d._handle_request({"service": "noauth", "method": "GET", "url": "https://example.com/x"})

    assert "authorization" not in seen["headers"]


def test_handle_request_marks_binary_for_octet_stream(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01\x02"
        )

    d = Daemon(
        auth_path=tmp_path / "auth.toml",
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )

    out = d._handle_request(
        {"service": "github", "method": "GET", "url": "https://example.com/blob"}
    )

    assert out["encoding"] == "binary"
    assert base64.b64decode(out["body_b64"]) == b"\x00\x01\x02"


def test_handle_request_passes_through_4xx(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, headers={"content-type": "application/json"}, content=b'{"error":"nope"}'
        )

    d = Daemon(
        auth_path=tmp_path / "auth.toml",
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )

    out = d._handle_request(
        {"service": "github", "method": "GET", "url": "https://api.github.com/x"}
    )

    assert out["status"] == 404
    assert base64.b64decode(out["body_b64"]) == b'{"error":"nope"}'


def test_handle_reload_returns_stub() -> None:
    d = Daemon(auth_path=Path("/dev/null"), stats_path=Path("/dev/null"))

    assert d._handle_reload({}) == {"loaded": [], "errors": []}
    assert d._handle_reload({"service": "github"}) == {"loaded": [], "errors": []}


def test_handle_request_passes_json_body_and_params(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = request.content
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    d = Daemon(
        auth_path=tmp_path / "auth.toml",
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )
    d._handle_request(
        {
            "service": "github",
            "method": "POST",
            "url": "https://api.github.com/issues",
            "params": {"state": "open"},
            "json": {"title": "hi"},
        }
    )

    assert seen["method"] == "POST"
    assert "state=open" in str(seen["url"])
    assert json.loads(seen["body"]) == {"title": "hi"}  # type: ignore[arg-type]


def test_handle_request_timeout_none_uses_default(tmp_path: Path) -> None:
    captured: dict[str, float] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        ext_timeout = request.extensions.get("timeout") or {}
        captured["connect"] = ext_timeout.get("connect", -1.0)
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    d = Daemon(
        auth_path=tmp_path / "auth.toml",
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )
    d._handle_request({"service": "github", "method": "GET", "url": "https://x/y", "timeout": None})
    assert captured["connect"] == 30.0

    captured.clear()
    d._handle_request({"service": "github", "method": "GET", "url": "https://x/y"})
    assert captured["connect"] == 30.0


def test_handle_request_timeout_explicit_value(tmp_path: Path) -> None:
    captured: dict[str, float] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        ext_timeout = request.extensions.get("timeout") or {}
        captured["connect"] = ext_timeout.get("connect", -1.0)
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    d = Daemon(
        auth_path=tmp_path / "auth.toml",
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )
    d._handle_request({"service": "github", "method": "GET", "url": "https://x/y", "timeout": 10})
    assert captured["connect"] == 10.0


def test_handle_request_env_key_normalizes_dashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRAFT_GOOGLE_DRIVE_TOKEN", "abc")
    seen: dict[str, str | None] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    d = Daemon(
        auth_path=tmp_path / "auth.toml",
        stats_path=tmp_path / "stats.jsonl",
        transport=_mock_transport(respond),
    )
    d._handle_request({"service": "google-drive", "method": "GET", "url": "https://x/y"})

    assert seen["auth"] == "Bearer abc"


def test_lookup_token_warns_when_auth_toml_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GRAFT_GITHUB_TOKEN", raising=False)
    auth = tmp_path / "auth.toml"
    auth.write_text("this is = not [valid toml", encoding="utf-8")

    d = Daemon(auth_path=auth, stats_path=tmp_path / "stats.jsonl")
    assert d._lookup_token("github") is None

    err = capsys.readouterr().err
    assert "auth.toml unreadable" in err


def test_lookup_token_caches_auth_toml_after_first_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GRAFT_GITHUB_TOKEN", raising=False)
    auth = tmp_path / "auth.toml"
    auth.write_text('[github]\ntoken = "xyz"\n', encoding="utf-8")

    reads: list[Path] = []
    original = Path.read_text

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == auth:
            reads.append(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    d = Daemon(auth_path=auth, stats_path=tmp_path / "stats.jsonl")
    assert d._lookup_token("github") == "xyz"
    assert d._lookup_token("github") == "xyz"
    assert d._lookup_token("github") == "xyz"

    assert len(reads) == 1


def test_dispatch_500_reason_does_not_leak_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unexpected exceptions surface only the type name; full message goes to stderr."""

    def boom(_body: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("super-secret-internal-detail")

    url, handle = _start_daemon_thread(tmp_path)
    try:
        handle.daemon._handle_reload = boom  # type: ignore[method-assign]
        with _direct_client() as c:
            resp = c.post(url + "/reload", json={})
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "InternalError"
        assert body["reason"] == "RuntimeError"
        assert "super-secret-internal-detail" not in body["reason"]
    finally:
        assert handle.daemon._server is not None
        handle.daemon._server.shutdown()
        handle.daemon._server.server_close()
        handle.thread.join(timeout=2.0)

    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "super-secret-internal-detail" in err


# ---------------------------------------------------------------------------
# Port-file lifecycle
# ---------------------------------------------------------------------------


def test_serve_aborts_when_port_file_holds_live_pid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    port_file = tmp_path / "daemon.port"
    port_file.write_text(f"{os.getpid()}:54321")

    with pytest.raises(SystemExit) as exc_info:
        serve(port=0, port_file=port_file, auth_path=tmp_path / "auth.toml")

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "already running" in err
    # Existing file untouched.
    assert port_file.read_text().startswith(f"{os.getpid()}:")


def test_serve_overwrites_dead_pid_port_file(tmp_path: Path) -> None:
    port_file = tmp_path / "daemon.port"
    # 99999999 is far above /proc/sys/kernel/pid_max defaults — effectively guaranteed dead.
    port_file.write_text("99999999:1")

    with _running_daemon(tmp_path) as base_url:
        # Port file got overwritten with our pid + actual port.
        content = port_file.read_text()
        pid_str, port_str = content.split(":")
        assert int(pid_str) == os.getpid()
        # Health check confirms server is live.
        with _direct_client() as client:
            r = client.get(f"{base_url}/health")
        assert r.status_code == 200
        assert int(port_str) > 0


# ---------------------------------------------------------------------------
# Real-server smoke (one fixture, hits /health + /stats)
# ---------------------------------------------------------------------------


class _ServerHandle:
    def __init__(self, daemon: Daemon, thread: threading.Thread) -> None:
        self.daemon = daemon
        self.thread = thread


def _start_daemon_thread(tmp_path: Path) -> tuple[str, _ServerHandle]:
    port_file = tmp_path / "daemon.port"
    auth_path = tmp_path / "auth.toml"
    stats_path = tmp_path / "stats.jsonl"
    daemon = Daemon(auth_path=auth_path, stats_path=stats_path)
    daemon.bind(host="127.0.0.1", port=0, port_file=port_file)
    assert daemon._server is not None
    server = daemon._server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not port_file.exists():
        time.sleep(0.01)
    return f"http://127.0.0.1:{daemon.actual_port}", _ServerHandle(daemon, thread)


@contextmanager
def _running_daemon(tmp_path: Path) -> Iterator[str]:
    url, handle = _start_daemon_thread(tmp_path)
    try:
        yield url
    finally:
        assert handle.daemon._server is not None
        handle.daemon._server.shutdown()
        handle.daemon._server.server_close()
        handle.thread.join(timeout=2.0)


@pytest.fixture
def daemon_url(tmp_path: Path) -> Iterator[str]:
    url, handle = _start_daemon_thread(tmp_path)
    try:
        yield url
    finally:
        assert handle.daemon._server is not None
        handle.daemon._server.shutdown()
        handle.daemon._server.server_close()
        handle.thread.join(timeout=2.0)


def _direct_client() -> httpx.Client:
    """No env-driven proxy — localhost wire tests must bypass any system http_proxy."""
    return httpx.Client(trust_env=False, timeout=2.0)


def test_health_endpoint_smoke(daemon_url: str) -> None:
    with _direct_client() as c:
        r = c.get(f"{daemon_url}/health")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["pid"] == os.getpid()
    assert isinstance(body["port"], int)
    assert "version" in body


def test_stats_endpoint_smoke_writes_file(daemon_url: str, tmp_path: Path) -> None:
    # The fixture-bound stats file lives at tmp_path/stats.jsonl per _start_daemon_thread.
    payload = {
        "service": "github",
        "helper": "list_issues",
        "ts": FIXED_ISO,
        "ok": True,
    }

    with _direct_client() as c:
        r = c.post(f"{daemon_url}/stats", json=payload)

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    stats_file = tmp_path / "stats.jsonl"
    assert stats_file.exists()
    lines = stats_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["helper"] == "list_issues"


def test_unknown_route_returns_404(daemon_url: str) -> None:
    with _direct_client() as c:
        r = c.get(f"{daemon_url}/no-such-thing")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "NotFound"


def test_request_with_invalid_json_body_returns_400(daemon_url: str) -> None:
    with _direct_client() as c:
        r = c.post(
            f"{daemon_url}/stats",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "BadRequest"
