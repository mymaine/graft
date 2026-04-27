"""Unit tests for graft.cli — 5 subcommands + argparse smoke."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from graft import cli
from graft.daemon import Daemon

FIXED_ISO = "2026-04-26T15:30:21+00:00"


@pytest.fixture
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    yield tmp_path


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_scaffold(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["init"])

    assert rc == 0
    assert (in_tmp / "helpers").is_dir()
    assert (in_tmp / "helpers" / "__init__.py").exists()
    assert (in_tmp / ".graft").is_dir()
    skill = in_tmp / "SKILL.md"
    assert skill.exists()
    text = skill.read_text(encoding="utf-8")
    assert text.startswith(f"# graft v{cli.VERSION}")
    assert "{{VERSION}}" not in text
    assert "graft initialized" in capsys.readouterr().err


def test_init_idempotent(in_tmp: Path) -> None:
    assert cli.main(["init"]) == 0
    # Pre-existing user content survives a second init only for files we don't own.
    (in_tmp / "helpers" / "github.py").write_text("# user code", encoding="utf-8")

    assert cli.main(["init"]) == 0
    assert (in_tmp / "helpers" / "github.py").read_text(encoding="utf-8") == "# user code"
    assert (in_tmp / "SKILL.md").exists()


def test_init_writes_helpers_init_with_eager_load(in_tmp: Path) -> None:
    """init must write helpers/__init__.py that eager-loads helpers via graft.loader."""
    assert cli.main(["init"]) == 0
    init_py = (in_tmp / "helpers" / "__init__.py").read_text(encoding="utf-8")
    assert "from graft import loader" in init_py
    assert "loader.load" in init_py


def test_read_helpers_init_returns_template_content() -> None:
    """Template lookup uses package data, not filesystem heuristics."""
    text = cli._read_helpers_init()
    assert "from graft import loader" in text
    assert "loader.load" in text


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def test_sync_regenerates_index(in_tmp: Path) -> None:
    cli.main(["init"])
    helpers = in_tmp / "helpers"
    (helpers / "foo.py").write_text('"""Foo service."""\n', encoding="utf-8")

    rc = cli.main(["sync"])

    assert rc == 0
    index = (helpers / "INDEX.md").read_text(encoding="utf-8")
    assert "foo.py" in index


def test_sync_warns_when_skill_md_differs_without_force(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["init"])
    skill = in_tmp / "SKILL.md"
    skill.write_text("EDITED BY USER\n", encoding="utf-8")

    rc = cli.main(["sync"])

    assert rc == 0
    assert skill.read_text(encoding="utf-8") == "EDITED BY USER\n"
    assert "differs" in capsys.readouterr().err


def test_sync_force_overwrites(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["init"])
    skill = in_tmp / "SKILL.md"
    skill.write_text("EDITED BY USER\n", encoding="utf-8")

    rc = cli.main(["sync", "--force"])

    assert rc == 0
    text = skill.read_text(encoding="utf-8")
    assert text.startswith(f"# graft v{cli.VERSION}")
    assert "updated" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_no_data(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["stats"])

    assert rc == 0
    assert "no stats" in capsys.readouterr().err


def test_stats_with_data_sorted_by_calls(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    graft_dir = in_tmp / ".graft"
    graft_dir.mkdir()
    lines = [
        {"service": "github", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
        {"service": "github", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
        {"service": "github", "helper": "get_repo", "ts": FIXED_ISO, "ok": False},
        {"service": "linear", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
    ]
    (graft_dir / "stats.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )

    rc = cli.main(["stats"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "github" in out
    assert "linear" in out
    # github (3 calls) ranks before linear (1 call).
    assert out.index("github") < out.index("linear")


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_reset_without_daemon_returns_1(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["reset", "github"])

    assert rc == 1
    assert "daemon not running" in capsys.readouterr().err


def test_reset_with_running_daemon_clears_circuit(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port_file = in_tmp / ".graft" / "daemon.port"
    auth_path = in_tmp / ".graft" / "auth.toml"
    stats_path = in_tmp / ".graft" / "stats.jsonl"
    daemon = Daemon(auth_path=auth_path, stats_path=stats_path)
    daemon.bind(host="127.0.0.1", port=0, port_file=port_file)
    assert daemon._server is not None
    server = daemon._server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not port_file.exists():
        time.sleep(0.01)

    monkeypatch.setattr(cli.loader, "PORT_FILE", port_file)
    try:
        # Prime the circuit so reset is observable.
        daemon.circuit.record_failure("github")
        daemon.circuit.record_failure("github")

        rc = cli.main(["reset", "github"])

        assert rc == 0
        assert "circuit reset" in capsys.readouterr().err
        # Counter cleared: next failure is count=1, action=raise.
        action = daemon.circuit.record_failure("github")
        assert action.count == 1
        assert action.action == "raise"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_reset_http_error_returns_1(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stale port file with no live daemon → connection refused → return 1."""
    port_file = in_tmp / ".graft" / "daemon.port"
    port_file.parent.mkdir(parents=True)
    # Live pid + a port nothing listens on.
    port_file.write_text(f"{os.getpid()}:1", encoding="utf-8")
    monkeypatch.setattr(cli.loader, "PORT_FILE", port_file)

    rc = cli.main(["reset", "github"])

    assert rc == 1
    assert "reset failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def test_serve_delegates_to_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, bool] = {}

    def fake_serve() -> None:
        called["serve"] = True

    monkeypatch.setattr(cli.daemon, "serve", fake_serve)

    rc = cli.main(["serve"])

    assert rc == 0
    assert called["serve"] is True


# ---------------------------------------------------------------------------
# argparse error handling
# ---------------------------------------------------------------------------


def test_unknown_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code == 2


def test_no_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# packaging / VERSION / line endings
# ---------------------------------------------------------------------------


def test_version_resolves_from_package_metadata() -> None:
    """VERSION single-source: importlib.metadata reflects pyproject.toml."""
    from importlib.metadata import version

    assert version("graft") == cli.VERSION


def test_read_template_returns_skill_md_content() -> None:
    """Template lookup uses package data, not filesystem heuristics."""
    text = cli._read_template()
    assert "{{VERSION}}" in text
    assert len(text) > 0


def test_sync_treats_crlf_target_as_unchanged(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CRLF SKILL.md whose normalized content matches the template is not flagged as drift."""
    cli.main(["init"])
    capsys.readouterr()
    skill = in_tmp / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8").replace("\n", "\r\n"), encoding="utf-8")

    rc = cli.main(["sync"])

    assert rc == 0
    assert "differs" not in capsys.readouterr().err
