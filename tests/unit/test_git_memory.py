"""Unit tests for graft.git_memory.

Real git repos in tmp_path; we exercise subprocess paths end-to-end so the
contract (only stage helpers/, dirty tree halts, env override) is verified
against actual git behavior — not mocks of our own wrappers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pytest import CaptureFixture, MonkeyPatch

from graft import git_memory
from graft.git_memory import commit_helpers, git_log_mtime, git_mv, should_commit


@pytest.fixture(autouse=True)
def _reset_warned_invalid() -> None:
    git_memory._warned_invalid.clear()


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    _run("init", "-q", "-b", "main", cwd=tmp_path)
    _run("config", "user.email", "test@example.com", cwd=tmp_path)
    _run("config", "user.name", "Test", cwd=tmp_path)
    _run("config", "commit.gpgsign", "false", cwd=tmp_path)
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    _run("add", "README.md", cwd=tmp_path)
    _run("commit", "-q", "-m", "init", cwd=tmp_path)
    return tmp_path


def _git_log_oneline(cwd: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--oneline"], cwd=cwd, capture_output=True, text=True, check=True
    )
    return out.stdout.strip().splitlines()


def _staged_files(cwd: Path) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip().splitlines()


def _unstaged_files(cwd: Path) -> list[str]:
    diff = subprocess.run(
        ["git", "diff", "--name-only"], cwd=cwd, capture_output=True, text=True, check=True
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in (diff.stdout + untracked.stdout).splitlines() if p]


def test_should_commit_disabled_when_env_zero(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "0")

    proceed, reason = should_commit(repo)

    assert proceed is False
    assert reason == "autocommit disabled"


def test_should_commit_clean_tree_default_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")

    proceed, reason = should_commit(repo)

    assert proceed is True
    assert reason is None


def test_should_commit_unset_env_treated_as_one(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.delenv("GRAFT_AUTOCOMMIT", raising=False)

    proceed, reason = should_commit(repo)

    assert proceed is True
    assert reason is None


def test_should_commit_invalid_env_warns_and_treats_as_one(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "yes")

    proceed, reason = should_commit(repo)

    captured = capsys.readouterr()
    assert proceed is True
    assert reason is None
    assert "GRAFT_AUTOCOMMIT" in captured.err
    assert "yes" in captured.err


def test_should_commit_dirty_tree_outside_helpers(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")

    proceed, reason = should_commit(repo)

    assert proceed is False
    assert reason is not None
    assert reason.startswith("dirty tree:")
    assert "README.md" in reason


def test_should_commit_dirty_tree_only_helpers_allows_commit(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    (repo / "helpers").mkdir()
    (repo / "helpers" / "foo.py").write_text("x = 1\n", encoding="utf-8")

    proceed, reason = should_commit(repo)

    assert proceed is True
    assert reason is None


def test_should_commit_untracked_outside_helpers_blocks(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    (repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")

    proceed, reason = should_commit(repo)

    assert proceed is False
    assert reason is not None
    assert "scratch.txt" in reason


def test_should_commit_truncates_to_three_files(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", "1")
    for n in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
        (repo / n).write_text("x\n", encoding="utf-8")

    proceed, reason = should_commit(repo)

    assert proceed is False
    assert reason is not None
    assert reason.endswith("...")
    listed = reason.removeprefix("dirty tree: ").removesuffix("...")
    assert len(listed.split(",")) == 3


def test_should_commit_skips_in_detached_head(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.delenv("GRAFT_AUTOCOMMIT", raising=False)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "--detach", sha], cwd=repo, check=True, capture_output=True)

    proceed, reason = should_commit(repo)

    assert proceed is False
    assert reason is not None
    assert "detached" in reason.lower()


def test_commit_helpers_creates_commit_with_message(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "helpers").mkdir()
    (repo / "helpers" / "foo.py").write_text("y = 2\n", encoding="utf-8")

    ok = commit_helpers("feat: add foo helper", repo)

    assert ok is True
    log = _git_log_oneline(repo)
    assert len(log) == 2
    assert "feat: add foo helper" in log[0]


def test_commit_helpers_idempotent_when_nothing_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    ok = commit_helpers("noop", repo)

    assert ok is True
    log = _git_log_oneline(repo)
    assert len(log) == 1


def test_commit_helpers_in_non_git_repo_returns_false(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    ok = commit_helpers("nope", not_a_repo)

    captured = capsys.readouterr()
    assert ok is False
    assert "git_memory" in captured.err


def test_commit_helpers_does_not_stage_outside_helpers(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "helpers").mkdir()
    (repo / "helpers" / "foo.py").write_text("y = 2\n", encoding="utf-8")
    (repo / "non_helpers.txt").write_text("dirty\n", encoding="utf-8")

    ok = commit_helpers("feat: helpers only", repo)

    assert ok is True
    log = _git_log_oneline(repo)
    assert len(log) == 2

    show = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    committed = [p for p in show.stdout.strip().splitlines() if p]
    assert committed == ["helpers/foo.py"]
    assert "non_helpers.txt" in _unstaged_files(repo)
    assert "non_helpers.txt" not in _staged_files(repo)


def test_commit_helpers_returns_false_when_helpers_missing_and_other_dirty(
    tmp_path: Path,
) -> None:
    """git add helpers/ on a non-existent path is a no-op; nothing should commit."""
    repo = _init_repo(tmp_path)
    (repo / "extra.txt").write_text("x\n", encoding="utf-8")

    ok = commit_helpers("noop", repo)

    assert ok is True
    log = _git_log_oneline(repo)
    assert len(log) == 1


def test_git_log_mtime_returns_epoch_for_committed_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "helpers").mkdir()
    (repo / "helpers" / "github.py").write_text("x = 1\n", encoding="utf-8")
    _run("add", "helpers/github.py", cwd=repo)
    _run("commit", "-q", "-m", "add github", cwd=repo)

    ts = git_log_mtime("helpers/github.py", repo)

    assert isinstance(ts, int)
    assert ts > 0


def test_git_log_mtime_none_for_untracked_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "helpers").mkdir()
    (repo / "helpers" / "fresh.py").write_text("x = 1\n", encoding="utf-8")

    assert git_log_mtime("helpers/fresh.py", repo) is None


def test_git_log_mtime_none_for_missing_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    assert git_log_mtime("helpers/nonexistent.py", repo) is None


def test_git_mv_moves_and_stages(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "helpers").mkdir()
    src = repo / "helpers" / "stale.py"
    src.write_text("y = 1\n", encoding="utf-8")
    _run("add", "helpers/stale.py", cwd=repo)
    _run("commit", "-q", "-m", "add stale", cwd=repo)
    (repo / "helpers" / "_archive").mkdir()
    dst = repo / "helpers" / "_archive" / "stale.py"

    ok = git_mv(src, dst, repo)

    assert ok is True
    assert not src.exists()
    assert dst.exists()
    assert "helpers/_archive/stale.py" in _staged_files(repo)


def test_git_mv_force_overwrites_existing_dst(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "helpers").mkdir()
    src = repo / "helpers" / "stale.py"
    src.write_text("new = 2\n", encoding="utf-8")
    arch = repo / "helpers" / "_archive"
    arch.mkdir()
    (arch / "stale.py").write_text("old = 1\n", encoding="utf-8")
    _run("add", "helpers/stale.py", "helpers/_archive/stale.py", cwd=repo)
    _run("commit", "-q", "-m", "seed", cwd=repo)
    dst = arch / "stale.py"

    ok = git_mv(src, dst, repo)

    assert ok is True
    assert dst.read_text(encoding="utf-8") == "new = 2\n"


def test_git_mv_returns_false_on_failure(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    repo = _init_repo(tmp_path)

    ok = git_mv(repo / "missing.py", repo / "dst.py", repo)

    assert ok is False
    assert "git_memory" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0"])
def test_should_commit_env_zero_short_circuits_before_git(
    tmp_path: Path, monkeypatch: MonkeyPatch, value: str
) -> None:
    """Even outside a git repo, env=0 must short-circuit cleanly."""
    monkeypatch.setenv("GRAFT_AUTOCOMMIT", value)
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    proceed, reason = should_commit(not_a_repo)

    assert proceed is False
    assert reason == "autocommit disabled"


def test_is_dirty_outside_helpers_clean_tree_returns_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    assert git_memory.is_dirty_outside_helpers(repo) is None


def test_is_dirty_outside_helpers_helpers_only_returns_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "helpers").mkdir()
    (repo / "helpers" / "foo.py").write_text("x = 1\n", encoding="utf-8")

    assert git_memory.is_dirty_outside_helpers(repo) is None


def test_is_dirty_outside_helpers_flags_foreign_change(tmp_path: Path) -> None:
    """Decoupled from autocommit: even with env=0, foreign dirt is reported."""
    repo = _init_repo(tmp_path)
    (repo / "scratch.txt").write_text("dirt\n", encoding="utf-8")

    reason = git_memory.is_dirty_outside_helpers(repo)

    assert reason is not None
    assert reason.startswith("dirty tree:")
    assert "scratch.txt" in reason
