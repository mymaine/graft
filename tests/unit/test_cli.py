"""Unit tests for graft.cli — subcommands + argparse smoke."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from graft import cli
from graft.daemon import Daemon

FIXED_ISO = "2026-04-26T15:30:21+00:00"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(cwd: Path) -> None:
    _git(cwd, "init", "-q", "-b", "main")
    _git(cwd, "config", "user.email", "t@example.com")
    _git(cwd, "config", "user.name", "T")
    _git(cwd, "config", "commit.gpgsign", "false")
    (cwd / "README.md").write_text("init\n", encoding="utf-8")
    (cwd / ".gitignore").write_text(".graft/\n", encoding="utf-8")
    _git(cwd, "add", "README.md", ".gitignore")
    _git(cwd, "commit", "-q", "-m", "init")


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
    skill = in_tmp / ".claude/skills/graft/SKILL.md"
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
    assert (in_tmp / ".claude/skills/graft/SKILL.md").exists()


def test_init_writes_helpers_init_with_eager_load(in_tmp: Path) -> None:
    """init must write helpers/__init__.py that eager-loads helpers via graft.loader."""
    assert cli.main(["init"]) == 0
    init_py = (in_tmp / "helpers" / "__init__.py").read_text(encoding="utf-8")
    assert "from graft import loader" in init_py
    assert "loader.load" in init_py


def test_read_helpers_init_returns_template_content() -> None:
    """Template lookup uses package data, not filesystem heuristics."""
    text = cli._read_pkg("init.tmpl")
    assert "from graft import loader" in text
    assert "loader.load" in text


def test_init_creates_gitignore_when_missing(in_tmp: Path) -> None:
    """case 1: no .gitignore → write all three needed entries."""
    assert cli.main(["init"]) == 0

    lines = (in_tmp / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".graft/" in lines
    assert "__pycache__/" in lines
    assert "*.pyc" in lines


def test_init_appends_only_missing_gitignore_entries(in_tmp: Path) -> None:
    """case 2: existing .gitignore keeps user content, only missing entries appended."""
    (in_tmp / ".gitignore").write_text("node_modules/\n.graft/\n", encoding="utf-8")

    assert cli.main(["init"]) == 0

    text = (in_tmp / ".gitignore").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines.count(".graft/") == 1  # not duplicated
    assert "node_modules/" in lines  # user content preserved
    assert "__pycache__/" in lines  # appended
    assert "*.pyc" in lines  # appended
    assert text.endswith("\n")


def test_init_leaves_complete_gitignore_untouched(in_tmp: Path) -> None:
    """case 3: all three entries already present → file is not rewritten."""
    original = "custom\n.graft/\n__pycache__/\n*.pyc\n"
    (in_tmp / ".gitignore").write_text(original, encoding="utf-8")

    assert cli.main(["init"]) == 0

    assert (in_tmp / ".gitignore").read_text(encoding="utf-8") == original


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
    skill = in_tmp / ".claude/skills/graft/SKILL.md"
    skill.write_text("EDITED BY USER\n", encoding="utf-8")

    rc = cli.main(["sync"])

    assert rc == 0
    assert skill.read_text(encoding="utf-8") == "EDITED BY USER\n"
    assert "differs" in capsys.readouterr().err


def test_sync_force_overwrites(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["init"])
    skill = in_tmp / ".claude/skills/graft/SKILL.md"
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
# hot
# ---------------------------------------------------------------------------


def test_hot_no_data(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["hot"])

    assert rc == 0
    assert "no stats" in capsys.readouterr().err


def _seed_hot_stats(in_tmp: Path) -> None:
    graft_dir = in_tmp / ".graft"
    graft_dir.mkdir()
    rows = [
        # github.list_issues x3 (1 error)
        {"service": "github", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
        {"service": "github", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
        {"service": "github", "helper": "list_issues", "ts": FIXED_ISO, "ok": False},
        # linear.list_issues x2
        {"service": "linear", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
        {"service": "linear", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
        # github.get_repo: same calls as linear.list_issues, more errors → ranks higher
        {"service": "github", "helper": "get_repo", "ts": FIXED_ISO, "ok": False},
        {"service": "github", "helper": "get_repo", "ts": FIXED_ISO, "ok": False},
        # notion.search x1
        {"service": "notion", "helper": "search", "ts": FIXED_ISO, "ok": True},
    ]
    (graft_dir / "stats.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_hot_default_limit_orders_by_calls_then_errors(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_hot_stats(in_tmp)

    rc = cli.main(["hot"])

    assert rc == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # header + 4 data rows
    assert len(lines) == 5
    body = lines[1:]
    assert body[0].startswith("github") and "list_issues" in body[0]
    # tiebreak: github.get_repo (2 errors) ranks above linear.list_issues (0 errors)
    assert body[1].startswith("github") and "get_repo" in body[1]
    assert body[2].startswith("linear") and "list_issues" in body[2]
    assert body[3].startswith("notion") and "search" in body[3]


def test_hot_custom_limit_truncates_rows(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_hot_stats(in_tmp)

    rc = cli.main(["hot", "--limit", "2"])

    assert rc == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 3  # header + 2


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
# prune
# ---------------------------------------------------------------------------


def _seed_helpers(repo: Path, names: list[str]) -> None:
    helpers = repo / "helpers"
    helpers.mkdir(exist_ok=True)
    for n in names:
        (helpers / f"{n}.py").write_text(f'"""{n}."""\n', encoding="utf-8")
    _git(repo, "add", "helpers/")
    _git(repo, "commit", "-q", "-m", "seed helpers")


def _write_stats(repo: Path, rows: list[dict[str, object]]) -> None:
    graft_dir = repo / ".graft"
    graft_dir.mkdir(exist_ok=True)
    (graft_dir / "stats.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_prune_no_helpers_dir(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["prune", "--stale", "90"])

    assert rc == 0
    assert "no helpers" in capsys.readouterr().err


def test_prune_dry_run_lists_stale_without_moving(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(in_tmp)
    _seed_helpers(in_tmp, ["github", "linear"])
    fresh = (datetime.now(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    stale = (datetime.now(UTC) - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    _write_stats(
        in_tmp,
        [
            {"service": "github", "helper": "list_issues", "ts": stale, "ok": True},
            {"service": "linear", "helper": "list_issues", "ts": fresh, "ok": True},
        ],
    )

    rc = cli.main(["prune", "--stale", "90"])

    assert rc == 0
    assert (in_tmp / "helpers" / "github.py").exists()
    assert not (in_tmp / "helpers" / "_archive").exists()
    out = capsys.readouterr().out
    assert "github" in out and "archive" in out
    assert "linear" in out and "keep" in out


def test_prune_apply_moves_and_commits(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(in_tmp)
    _seed_helpers(in_tmp, ["github"])
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    stale = (datetime.now(UTC) - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    _write_stats(in_tmp, [{"service": "github", "helper": "x", "ts": stale, "ok": True}])

    rc = cli.main(["prune", "--stale", "90", "--apply"])

    assert rc == 0
    assert not (in_tmp / "helpers" / "github.py").exists()
    assert (in_tmp / "helpers" / "_archive" / "github.py").exists()
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=in_tmp, capture_output=True, text=True, check=True
    ).stdout
    assert "archive stale helper github" in log


def test_prune_apply_aborts_on_commit_failure(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(in_tmp)
    _seed_helpers(in_tmp, ["a", "b"])
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    stale = (datetime.now(UTC) - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    _write_stats(
        in_tmp,
        [
            {"service": "a", "helper": "x", "ts": stale, "ok": True},
            {"service": "b", "helper": "x", "ts": stale, "ok": True},
        ],
    )
    monkeypatch.setattr("graft.git_memory.commit_helpers", lambda *a, **kw: False)

    rc = cli.main(["prune", "--stale", "90", "--apply"])

    assert rc == 1
    assert "commit failed for a" in capsys.readouterr().err
    assert (in_tmp / "helpers" / "_archive" / "a.py").exists()
    assert (in_tmp / "helpers" / "_archive" / "b.py").exists() is False


def test_prune_apply_uses_git_log_mtime_when_no_stats(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(in_tmp)
    _seed_helpers(in_tmp, ["old"])
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    # Backdate the seed commit by amending the committer/author date.
    backdated = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%S")
    env = {
        **os.environ,
        "GIT_COMMITTER_DATE": backdated,
        "GIT_AUTHOR_DATE": backdated,
    }
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "--date", backdated],
        cwd=in_tmp,
        env=env,
        check=True,
        capture_output=True,
    )

    rc = cli.main(["prune", "--stale", "90", "--apply"])

    assert rc == 0
    assert (in_tmp / "helpers" / "_archive" / "old.py").exists()


def test_prune_skips_uncommitted_helper(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _init_repo(in_tmp)
    helpers = in_tmp / "helpers"
    helpers.mkdir()
    (helpers / "fresh.py").write_text('"""fresh."""\n', encoding="utf-8")
    # Not committed; no stats.

    rc = cli.main(["prune", "--stale", "90"])

    assert rc == 0
    err = capsys.readouterr().err.lower()
    assert "no stale" in err or "no helpers" in err


def test_prune_apply_overwrites_existing_archive(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(in_tmp)
    helpers = in_tmp / "helpers"
    helpers.mkdir()
    (helpers / "github.py").write_text('"""new."""\n', encoding="utf-8")
    arch = helpers / "_archive"
    arch.mkdir()
    (arch / "github.py").write_text('"""old."""\n', encoding="utf-8")
    _git(in_tmp, "add", "helpers/")
    _git(in_tmp, "commit", "-q", "-m", "seed")
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    stale = (datetime.now(UTC) - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    _write_stats(in_tmp, [{"service": "github", "helper": "x", "ts": stale, "ok": True}])

    rc = cli.main(["prune", "--stale", "90", "--apply"])

    assert rc == 0
    assert (arch / "github.py").read_text(encoding="utf-8") == '"""new."""\n'
    assert "exists" in capsys.readouterr().err.lower()


def test_prune_apply_respects_autocommit_zero(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(in_tmp)
    _seed_helpers(in_tmp, ["github"])
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "0")
    stale = (datetime.now(UTC) - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    _write_stats(in_tmp, [{"service": "github", "helper": "x", "ts": stale, "ok": True}])

    rc = cli.main(["prune", "--stale", "90", "--apply"])

    assert rc == 0
    log_before = subprocess.run(
        ["git", "log", "--oneline"], cwd=in_tmp, capture_output=True, text=True, check=True
    ).stdout
    # Mv applied (file moved) but not committed.
    assert (in_tmp / "helpers" / "_archive" / "github.py").exists()
    assert "archive stale helper github" not in log_before
    assert "manual" in capsys.readouterr().err.lower()


def test_prune_apply_dirty_tree_pauses(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(in_tmp)
    _seed_helpers(in_tmp, ["github"])
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    (in_tmp / "scratch.txt").write_text("dirt\n", encoding="utf-8")
    stale = (datetime.now(UTC) - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    _write_stats(in_tmp, [{"service": "github", "helper": "x", "ts": stale, "ok": True}])

    rc = cli.main(["prune", "--stale", "90", "--apply"])

    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "dirty" in err or "scratch.txt" in err
    assert (in_tmp / "helpers" / "github.py").exists()


def test_prune_apply_dirty_tree_pauses_even_when_autocommit_disabled(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dirty-tree guard must run regardless of GRAFT_AUTOCOMMIT — otherwise git mv
    would drag the user's foreign changes into _archive/ when autocommit=0."""
    _init_repo(in_tmp)
    _seed_helpers(in_tmp, ["github"])
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "0")
    (in_tmp / "scratch.txt").write_text("dirt\n", encoding="utf-8")
    stale = (datetime.now(UTC) - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    _write_stats(in_tmp, [{"service": "github", "helper": "x", "ts": stale, "ok": True}])

    rc = cli.main(["prune", "--stale", "90", "--apply"])

    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "prune paused" in err
    assert "dirty tree" in err
    # The helper must still be in place — no git mv should have run.
    assert (in_tmp / "helpers" / "github.py").exists()
    assert not (in_tmp / "helpers" / "_archive" / "github.py").exists()


def test_prune_default_stale_is_90(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _init_repo(in_tmp)
    _seed_helpers(in_tmp, ["github"])
    age80 = (datetime.now(UTC) - timedelta(days=80)).isoformat().replace("+00:00", "Z")
    _write_stats(in_tmp, [{"service": "github", "helper": "x", "ts": age80, "ok": True}])

    rc = cli.main(["prune"])

    assert rc == 0
    out = capsys.readouterr().out
    # 80 days < default 90 → keep
    assert "keep" in out


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
    text = cli._read_pkg("SKILL.md")
    assert "{{VERSION}}" in text
    assert len(text) > 0


def test_sync_treats_crlf_target_as_unchanged(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CRLF SKILL.md whose normalized content matches the template is not flagged as drift."""
    cli.main(["init"])
    capsys.readouterr()
    skill = in_tmp / ".claude/skills/graft/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8").replace("\n", "\r\n"), encoding="utf-8")

    rc = cli.main(["sync"])

    assert rc == 0
    assert "differs" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


_HELPER_SRC = '''"""github helpers."""

from __future__ import annotations


def list_issues(owner: str) -> None:
    """List issues.

    Generalization:
        any owner.
    """


def get_repo(owner: str) -> None:
    """Get repo.

    Generalization:
        any owner.
    """


async def search_code(q: str) -> None:
    """Async search.

    Generalization:
        any query.
    """


def list_pulls(owner: str) -> None:
    """List pulls.

    Generalization:
        any owner.
    """


def create_issue(owner: str) -> None:
    """Create issue.

    Generalization:
        any owner.
    """


def _private_helper() -> None:
    """Skipped because of underscore prefix."""


class Client:
    def hidden_method(self) -> None:
        """Methods inside classes are not public helpers."""
'''


def _write_helper(in_tmp: Path, name: str, src: str) -> Path:
    helpers = in_tmp / "helpers"
    helpers.mkdir(exist_ok=True)
    target = helpers / f"{name}.py"
    target.write_text(src, encoding="utf-8")
    return target


def _seed_inspect_stats(in_tmp: Path) -> None:
    graft_dir = in_tmp / ".graft"
    graft_dir.mkdir(exist_ok=True)
    rows = [
        {"service": "github", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
        {"service": "github", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
        {"service": "github", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
        {"service": "github", "helper": "get_repo", "ts": FIXED_ISO, "ok": False},
        {"service": "github", "helper": "get_repo", "ts": FIXED_ISO, "ok": False},
        {"service": "github", "helper": "search_code", "ts": FIXED_ISO, "ok": True},
        # noise from a different service: must be filtered out.
        {"service": "linear", "helper": "list_issues", "ts": FIXED_ISO, "ok": True},
    ]
    (graft_dir / "stats.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_inspect_missing_service_returns_1(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["inspect", "github"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "helper file not found" in err
    assert "helpers/github.py" in err


def test_inspect_lists_all_public_helpers_with_stats(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_helper(in_tmp, "github", _HELPER_SRC)
    _seed_inspect_stats(in_tmp)

    rc = cli.main(["inspect", "github"])

    assert rc == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # title + blank + header + 5 helper rows -> 7 non-empty
    assert lines[0].startswith("service: github")
    assert "5 helpers" in lines[0]
    assert "6 calls" in lines[0]
    assert "2 errors" in lines[0]

    body = lines[2:]
    assert len(body) == 5
    # Order: list_issues(3) -> get_repo(2 calls, 2 errors) -> search_code(1) ->
    # then unused alphabetical: create_issue, list_pulls.
    assert body[0].split()[0] == "list_issues"
    assert body[1].split()[0] == "get_repo"
    assert body[2].split()[0] == "search_code"
    assert body[3].split()[0] == "create_issue"
    assert body[4].split()[0] == "list_pulls"
    # Unused helpers carry calls=0 and last=N/A.
    assert "N/A" in body[3]
    assert "N/A" in body[4]
    # Private + class methods are excluded.
    assert "_private_helper" not in out
    assert "hidden_method" not in out


def test_inspect_skips_private_async_and_class_methods(in_tmp: Path) -> None:
    """ast walk: only top-level def/async def, no underscore prefix, no class methods."""
    target = _write_helper(in_tmp, "github", _HELPER_SRC)
    names = cli._public_helpers_in(target)

    assert names == ["list_issues", "get_repo", "search_code", "list_pulls", "create_issue"]


def test_inspect_handles_zero_public_helpers(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_helper(in_tmp, "empty", '"""no public helpers here."""\n\ndef _hidden() -> None: ...\n')

    rc = cli.main(["inspect", "empty"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "no public helpers in empty.py" in captured.err
    assert "no public helpers" not in captured.out


def test_inspect_with_no_stats_shows_all_zero(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_helper(in_tmp, "github", _HELPER_SRC)

    rc = cli.main(["inspect", "github"])

    assert rc == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert "0 calls" in lines[0]
    assert "0 errors" in lines[0]
    body = lines[2:]
    # All-zero helpers sort alphabetically.
    assert [ln.split()[0] for ln in body] == sorted(
        ["list_issues", "get_repo", "search_code", "list_pulls", "create_issue"]
    )
    assert all("N/A" in ln for ln in body)


def test_inspect_syntax_error_returns_1(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_helper(in_tmp, "broken", "def oops(:\n")

    rc = cli.main(["inspect", "broken"])

    assert rc == 1
    assert "helpers/broken.py" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# add (registry pull)
# ---------------------------------------------------------------------------


def test_add_delegates_to_registry_install(in_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_install(
        service: str,
        project_dir: Path,
        registry_url: str | None,
        *,
        force: bool = False,
    ) -> int:
        captured.update(
            service=service,
            project_dir=project_dir,
            registry_url=registry_url,
            force=force,
        )
        return 0

    monkeypatch.setattr(cli.registry, "install", fake_install)

    rc = cli.main(["add", "echo", "--registry", "file:///tmp/r", "--force"])

    assert rc == 0
    assert captured == {
        "service": "echo",
        "project_dir": in_tmp,
        "registry_url": "file:///tmp/r",
        "force": True,
    }


def test_add_passes_none_when_registry_flag_absent(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_install(
        service: str,
        project_dir: Path,
        registry_url: str | None,
        *,
        force: bool = False,
    ) -> int:
        captured["registry_url"] = registry_url
        captured["force"] = force
        return 0

    monkeypatch.setattr(cli.registry, "install", fake_install)

    rc = cli.main(["add", "echo"])

    assert rc == 0
    assert captured == {"registry_url": None, "force": False}


def test_add_propagates_install_exit_code(in_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.registry, "install", lambda *a, **k: 1)

    rc = cli.main(["add", "echo", "--registry", "file:///tmp/r"])

    assert rc == 1
