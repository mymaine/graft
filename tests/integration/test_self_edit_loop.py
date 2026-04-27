"""End-to-end integration tests for the self-edit loop.

These tests wire real daemon threads to the real loader/validator/stats/git_memory
modules and verify the contract spec.md describes:

  Case 1 — happy path: import helpers.X via __init__.py eager-load -> wrap ->
                       call -> stats append (per-function helper name).
  Case 2 — validator fail then recovery: bad helper rejected, fixed helper loads.
  Case 3 — loader.load() triggers git_memory auto-commit on clean tree.
  Case 4 — DaemonNotRunning when no daemon is listening.
  Case 5 — skill.generate_index reflects helpers + stats after a real call.
  Case 6 — broken helper fails silently in eager-load; other helpers still work.

External HTTP is intercepted via httpx.MockTransport (so daemon -> upstream is
mocked), but loader -> daemon -> stats is the real wire.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from graft import git_memory, loader, skill, stats
from graft.daemon import Daemon
from graft.loader import DaemonNotRunning, HelperLoadError

MockHandler = Callable[[httpx.Request], httpx.Response]


# ---------------------------------------------------------------------------
# Shared fixture: real daemon thread + initialized git repo + scaffold
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_git_repo(tmp_path: Path) -> None:
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "test@graft", cwd=tmp_path)
    _git("config", "user.name", "test", cwd=tmp_path)
    _git("config", "commit.gpgsign", "false", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text("*.pyc\n__pycache__/\n.graft/\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=tmp_path)
    _git("commit", "-q", "-m", "initial", cwd=tmp_path)


def _scaffold(tmp_path: Path) -> None:
    (tmp_path / "helpers").mkdir()
    (tmp_path / "helpers" / "__init__.py").touch()
    (tmp_path / ".graft").mkdir()


@contextmanager
def _running_graft(
    tmp_path: Path, mock_handler: MockHandler | None = None
) -> Iterator[tuple[Path, Path, Daemon]]:
    """Stand up a real daemon thread bound to the tmp project tree.

    Returns (project_root, port_file_path, daemon_instance). The daemon's
    outbound httpx transport is replaced with MockTransport when a handler
    is supplied — useful for cases that exercise context.request().
    """
    _init_git_repo(tmp_path)
    _scaffold(tmp_path)

    port_file = tmp_path / ".graft" / "daemon.port"
    auth_path = tmp_path / ".graft" / "auth.toml"
    stats_path = tmp_path / ".graft" / "stats.jsonl"

    transport: httpx.BaseTransport | None = (
        httpx.MockTransport(mock_handler) if mock_handler is not None else None
    )
    daemon_inst = Daemon(auth_path=auth_path, stats_path=stats_path, transport=transport)
    daemon_inst.bind(host="127.0.0.1", port=0, port_file=port_file)
    assert daemon_inst._server is not None
    server = daemon_inst._server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    deadline = time.time() + 3.0
    while time.time() < deadline and not port_file.exists():
        time.sleep(0.01)
    assert port_file.exists(), "daemon failed to write port file"

    try:
        yield tmp_path, port_file, daemon_inst
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        for name in list(sys.modules):
            if name.startswith("helpers."):
                del sys.modules[name]


# ---------------------------------------------------------------------------
# Mock upstream handlers
# ---------------------------------------------------------------------------


def _github_mock(request: httpx.Request) -> httpx.Response:
    if "/repos/" in request.url.path and request.url.path.endswith("/issues"):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=[{"number": 1, "title": "test"}, {"number": 2, "title": "two"}],
        )
    return httpx.Response(404, json={"error": "not found"})


# ---------------------------------------------------------------------------
# Helper source bodies
# ---------------------------------------------------------------------------


VALID_GITHUB_HELPER = textwrap.dedent(
    '''
    """GitHub helpers."""
    from graft.context import request


    def list_issues(owner: str, repo: str) -> list[dict]:
        """List GitHub issues for a repository.

        Generalization:
            Works for any (owner, repo). Variant: list_issues("python", "cpython").
            Not applicable: GHE on custom domains.
        """
        return request(
            "github",
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/issues",
        ).json()
    '''
).lstrip()


INVALID_LINEAR_HELPER = textwrap.dedent(
    '''
    """Linear helpers."""
    from graft.context import request


    def list_issues() -> list:
        """No Generalization section here."""
        return []
    '''
).lstrip()


VALID_LINEAR_HELPER = textwrap.dedent(
    '''
    """Linear helpers."""
    from graft.context import request


    def list_issues() -> list:
        """List issues.

        Generalization:
            Works for any team.
            Variant: list_issues()
            Not applicable: archived workspaces.
        """
        return []
    '''
).lstrip()


# ---------------------------------------------------------------------------
# Case 1 — full self-edit loop
# ---------------------------------------------------------------------------


def test_full_self_edit_loop_writes_stats_for_real_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Natural `from helpers.github import list_issues` triggers eager-load wrap.

    Verifies AC-3 per-function granularity is preserved when agents bypass
    loader.load() and use Python's natural import path.
    """
    with _running_graft(tmp_path, mock_handler=_github_mock) as (root, port_file, _):
        (root / "helpers" / "github.py").write_text(VALID_GITHUB_HELPER, encoding="utf-8")

        from graft.cli import _read_helpers_init

        (root / "helpers" / "__init__.py").write_text(_read_helpers_init(), encoding="utf-8")

        monkeypatch.chdir(root)
        monkeypatch.setattr(loader, "PORT_FILE", port_file)
        sys.path.insert(0, str(root))
        try:
            for k in list(sys.modules):
                if k.startswith("helpers"):
                    sys.modules.pop(k)
            from helpers.github import list_issues

            result = list_issues("anthropics", "claude-code")
        finally:
            sys.path.remove(str(root))

        assert isinstance(result, list)
        assert result[0]["number"] == 1

        stats_file = root / ".graft" / "stats.jsonl"
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if stats_file.exists() and stats_file.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.02)

        agg = stats.aggregate(stats_file)
        assert "github" in agg
        gh = agg["github"]
        assert gh.total_calls >= 1
        assert gh.helper_count == 1
        assert gh.errors == 0

        # Crucial: helper name is the per-function "list_issues", not generic.
        helpers_seen = {
            json.loads(line)["helper"]
            for line in stats_file.read_text(encoding="utf-8").splitlines()
            if line
        }
        assert "list_issues" in helpers_seen


# ---------------------------------------------------------------------------
# Case 2 — validator fail then recovery (loader + daemon circuit interplay)
# ---------------------------------------------------------------------------


def test_validator_failure_then_rewrite_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad helper raises HelperLoadError; fixed helper loads. Circuit resets on success."""
    monkeypatch.chdir(tmp_path)
    with _running_graft(tmp_path) as (root, port_file, daemon_inst):
        (root / "helpers" / "linear.py").write_text(INVALID_LINEAR_HELPER, encoding="utf-8")

        with pytest.raises(HelperLoadError) as exc_info:
            loader.load("linear", helpers_dir=root / "helpers", port_file=port_file)
        assert "Generalization" in exc_info.value.reason
        # circuit recorded one failure
        assert daemon_inst.circuit._counts.get("linear") == 1

        # rewrite with Generalization
        (root / "helpers" / "linear.py").write_text(VALID_LINEAR_HELPER, encoding="utf-8")
        # purge cached module so import reloads from disk
        sys.modules.pop("helpers.linear", None)

        module = loader.load("linear", helpers_dir=root / "helpers", port_file=port_file)
        assert callable(module.list_issues)
        # success cleared the circuit counter
        assert "linear" not in daemon_inst.circuit._counts


# ---------------------------------------------------------------------------
# Case 3 — git_memory dirty-tree boundary + commit_helpers
# ---------------------------------------------------------------------------


def test_load_triggers_auto_commit_on_clean_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """loader.load() invokes git_memory.should_commit + commit_helpers itself."""
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    with _running_graft(tmp_path) as (root, port_file, _):
        (root / "helpers" / "github.py").write_text(VALID_GITHUB_HELPER, encoding="utf-8")

        loader.load("github", helpers_dir=root / "helpers", port_file=port_file)

        log = subprocess.run(
            ["git", "log", "--oneline", "--", "helpers/"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "graft: load github" in log
        # spec.md AC-5: commit message 含 service + 函數名
        assert "list_issues" in log

        # helpers/ files committed; nothing outside helpers/.
        show = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        committed = [p for p in show if p]
        assert all(p.startswith("helpers/") for p in committed)


def test_load_skips_auto_commit_on_dirty_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dirty tree (non-helpers/ change) blocks auto-commit; helpers/ stays in worktree."""
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    with _running_graft(tmp_path) as (root, port_file, _):
        (root / "helpers" / "github.py").write_text(VALID_GITHUB_HELPER, encoding="utf-8")
        (root / "scratch.txt").write_text("user notes", encoding="utf-8")

        proceed, reason = git_memory.should_commit(cwd=root)
        assert proceed is False
        assert reason is not None and reason.startswith("dirty tree:")
        assert "scratch.txt" in reason

        loader.load("github", helpers_dir=root / "helpers", port_file=port_file)

        log = subprocess.run(
            ["git", "log", "--oneline", "--", "helpers/"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "graft: load github" not in log


# ---------------------------------------------------------------------------
# Case 4 — DaemonNotRunning when daemon never started
# ---------------------------------------------------------------------------


def test_load_without_daemon_raises_daemon_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loader synthesizes DaemonNotRunning client-side when port file absent."""
    monkeypatch.chdir(tmp_path)
    helpers_dir = tmp_path / "helpers"
    helpers_dir.mkdir()
    (helpers_dir / "__init__.py").touch()
    (helpers_dir / "github.py").write_text(VALID_GITHUB_HELPER, encoding="utf-8")
    port_file = tmp_path / ".graft" / "daemon.port"

    with pytest.raises(DaemonNotRunning) as exc:
        loader.load("github", helpers_dir=helpers_dir, port_file=port_file)
    assert exc.value.source == "client"


# ---------------------------------------------------------------------------
# Case 5 — INDEX.md generation reflects helpers + stats after a real call
# ---------------------------------------------------------------------------


def test_generate_index_after_real_call_reflects_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """skill.generate_index joins on-disk helpers with stats.jsonl from a real call."""
    monkeypatch.chdir(tmp_path)
    with _running_graft(tmp_path, mock_handler=_github_mock) as (root, port_file, _):
        helpers_dir = root / "helpers"
        stats_path = root / ".graft" / "stats.jsonl"
        (helpers_dir / "github.py").write_text(VALID_GITHUB_HELPER, encoding="utf-8")

        module = loader.load("github", helpers_dir=helpers_dir, port_file=port_file)
        module.list_issues("anthropics", "claude-code")

        # Allow the asynchronous /stats POST to finish writing.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if stats_path.exists() and stats_path.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.02)

        index = skill.generate_index(helpers_dir, stats_path)
        assert "github.py" in index
        assert "GitHub helpers." in index
        assert "1 helpers" in index
        assert "1 calls" in index or "2 calls" in index


# ---------------------------------------------------------------------------
# Case 6 — broken helper does not block siblings during eager-load
# ---------------------------------------------------------------------------


def test_broken_helper_does_not_block_other_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """helpers/__init__.py captures per-file load errors into a sentinel so
    siblings remain importable; only touching the broken module raises."""
    with _running_graft(tmp_path, mock_handler=_github_mock) as (root, port_file, _):
        (root / "helpers" / "github.py").write_text(VALID_GITHUB_HELPER, encoding="utf-8")
        (root / "helpers" / "broken.py").write_text(INVALID_LINEAR_HELPER, encoding="utf-8")

        from graft.cli import _read_helpers_init

        (root / "helpers" / "__init__.py").write_text(_read_helpers_init(), encoding="utf-8")

        monkeypatch.chdir(root)
        monkeypatch.setattr(loader, "PORT_FILE", port_file)
        sys.path.insert(0, str(root))
        try:
            for k in list(sys.modules):
                if k.startswith("helpers"):
                    sys.modules.pop(k)
            import helpers  # noqa: F401
            from helpers.github import list_issues

            result = list_issues("anthropics", "claude-code")
        finally:
            sys.path.remove(str(root))

        assert isinstance(result, list)
        assert result[0]["number"] == 1


# ---------------------------------------------------------------------------
# Case 7 — invalid helper raises HelperLoadError on natural import
# ---------------------------------------------------------------------------


def test_invalid_helper_raises_on_natural_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid helper (missing Generalization) must raise HelperLoadError on
    natural ``from helpers.X import Y`` — not silently fall through to a naked
    import that bypasses the validator/wrap pipeline.
    """
    with _running_graft(tmp_path, mock_handler=_github_mock) as (root, port_file, _):
        (root / "helpers" / "github.py").write_text(VALID_GITHUB_HELPER, encoding="utf-8")
        (root / "helpers" / "broken.py").write_text(INVALID_LINEAR_HELPER, encoding="utf-8")

        from graft.cli import _read_helpers_init

        (root / "helpers" / "__init__.py").write_text(_read_helpers_init(), encoding="utf-8")

        monkeypatch.chdir(root)
        monkeypatch.setattr(loader, "PORT_FILE", port_file)
        sys.path.insert(0, str(root))
        try:
            for k in list(sys.modules):
                if k.startswith("helpers"):
                    sys.modules.pop(k)
            # sibling import still works
            from helpers.github import list_issues

            assert list_issues("anthropics", "claude-code")[0]["number"] == 1

            # touching the invalid module surfaces the captured load error
            with pytest.raises(HelperLoadError) as exc_info:
                from helpers.broken import foo  # noqa: F401
            assert "Generalization" in exc_info.value.reason
        finally:
            sys.path.remove(str(root))
