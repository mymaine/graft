"""Auto-commit helpers/ via git subprocess. Dirty-tree aware, env-gated, fail-soft."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ENV_VAR = "GRAFT_AUTOCOMMIT"
HELPERS = "helpers/"
_warned_invalid: set[str] = set()


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _warn(cmd: list[str], result: subprocess.CompletedProcess[str]) -> None:
    head = " ".join(cmd[:2])
    print(f"warning: git_memory: {head} failed: {result.stderr.strip()}", file=sys.stderr)


def _autocommit_enabled() -> bool:
    raw = os.environ.get(ENV_VAR, "1")
    if raw in ("0", "1"):
        return raw == "1"
    if raw not in _warned_invalid:
        _warned_invalid.add(raw)
        print(
            f"warning: git_memory: {ENV_VAR}={raw!r} not in (0,1); treating as 1",
            file=sys.stderr,
        )
    return True


def _on_branch(cwd: Path) -> bool:
    return _run(["git", "symbolic-ref", "-q", "HEAD"], cwd).returncode == 0


def _changed_paths(cwd: Path) -> list[str] | str:
    """Return changed paths, or an error reason string on git failure."""
    diff = _run(["git", "diff", "--name-only", "HEAD", "--"], cwd)
    if diff.returncode != 0:
        _warn(["git", "diff"], diff)
        return "git status unavailable"
    untracked = _run(["git", "ls-files", "--others", "--exclude-standard"], cwd)
    if untracked.returncode != 0:
        _warn(["git", "ls-files"], untracked)
        return "git status unavailable"
    lines = (diff.stdout + untracked.stdout).splitlines()
    return list(dict.fromkeys(line for line in lines if line))


def should_commit(cwd: Path | None = None) -> tuple[bool, str | None]:
    """Decide whether daemon may auto-commit. Skip on env=0, detached HEAD, or non-helpers/ dirt."""
    cwd = cwd if cwd is not None else Path.cwd()
    if not _autocommit_enabled():
        return False, "autocommit disabled"
    if not _on_branch(cwd):
        return False, "detached HEAD"
    result = _changed_paths(cwd)
    if isinstance(result, str):
        return False, result
    foreign = [p for p in result if not p.startswith(HELPERS)]
    if foreign:
        shown = ",".join(foreign[:3])
        suffix = "..." if len(foreign) > 3 else ""
        return False, f"dirty tree: {shown}{suffix}"
    return True, None


def commit_helpers(message: str, cwd: Path | None = None) -> bool:
    """Stage only helpers/ and commit. Idempotent when nothing staged. Fails soft."""
    cwd = cwd if cwd is not None else Path.cwd()
    if (cwd / "helpers").is_dir():
        add = _run(["git", "add", "--", HELPERS], cwd)
        if add.returncode != 0:
            _warn(["git", "add"], add)
            return False
    staged = _run(["git", "diff", "--cached", "--name-only"], cwd)
    if staged.returncode != 0:
        _warn(["git", "diff"], staged)
        return False
    if not staged.stdout.strip():
        return True
    commit = _run(["git", "commit", "-m", message], cwd)
    if commit.returncode != 0:
        _warn(["git", "commit"], commit)
        return False
    return True
