"""Unit tests for graft.registry.

Registry client clones a git repo (file:// or https://), parses manifest.json,
copies the named helper into <project>/helpers/<service>.py after running the
ast validator. Tests use file:// URLs over real local git repos in tmp_path.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from graft import registry

_GOOD_HELPER = '''"""echo helper."""

from graft.context import request


def echo_get(path: str = "/anything") -> dict:
    """Echo a GET.

    Generalization:
        Works for any path under /anything/*.
    """
    return request("echo", "GET", f"https://httpbin.org{path}").json()
'''

_BAD_HELPER = '''"""bad helper missing Generalization."""


def echo_get(path: str = "/anything") -> dict:
    """No marker section here."""
    return {}
'''


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_registry(
    tmp_path: Path,
    *,
    services: dict[str, dict[str, object]] | None = None,
    helper_src: str = _GOOD_HELPER,
    write_helper: bool = True,
) -> Path:
    """Build a tiny git registry under tmp_path/registry. Returns its absolute Path."""
    repo = tmp_path / "registry"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "r@example.com")
    _git(repo, "config", "user.name", "R")
    _git(repo, "config", "commit.gpgsign", "false")
    if services is None:
        services = {
            "echo": {
                "version": "0.1.0",
                "path": "helpers/echo/echo.py",
                "description": "echo dogfood",
                "summary_for_index": "echo",
                "auth_required": False,
                "tags": ["dogfood"],
            }
        }
    manifest = {"$schema_version": "1", "services": services}
    (repo / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if write_helper:
        for meta in services.values():
            target = repo / str(meta["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(helper_src, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    yield proj


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_install_writes_helper_from_file_url(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry(tmp_path)

    rc = registry.install("echo", project, f"file://{repo}")

    assert rc == 0
    written = project / "helpers" / "echo.py"
    assert written.exists()
    assert "echo_get" in written.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "added" in out
    assert "helpers/echo.py" in out


def test_install_returns_1_when_service_missing_from_manifest(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry(tmp_path)

    rc = registry.install("nope", project, f"file://{repo}")

    assert rc == 1
    err = capsys.readouterr().err
    assert "nope" in err
    assert "manifest" in err
    assert not (project / "helpers" / "nope.py").exists()


def test_install_returns_1_when_registry_path_missing(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    services = {
        "echo": {
            "version": "0.1.0",
            "path": "helpers/echo/echo.py",
            "description": "x",
            "summary_for_index": "x",
            "auth_required": False,
            "tags": [],
        }
    }
    repo = _make_registry(tmp_path, services=services, write_helper=False)
    # add a placeholder file so commit succeeds, then verify install fails
    (repo / "placeholder").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "placeholder")

    rc = registry.install("echo", project, f"file://{repo}")

    assert rc == 1
    err = capsys.readouterr().err
    assert "registry path missing" in err
    assert "helpers/echo/echo.py" in err


def test_install_returns_1_when_helper_fails_validator(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry(tmp_path, helper_src=_BAD_HELPER)

    rc = registry.install("echo", project, f"file://{repo}")

    assert rc == 1
    err = capsys.readouterr().err
    assert "validator" in err
    assert "Generalization" in err
    assert not (project / "helpers" / "echo.py").exists()


def test_install_refuses_to_overwrite_existing_helper_without_force(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry(tmp_path)
    (project / "helpers").mkdir()
    existing = project / "helpers" / "echo.py"
    existing.write_text("# user edit\n", encoding="utf-8")

    rc = registry.install("echo", project, f"file://{repo}")

    assert rc == 1
    assert existing.read_text(encoding="utf-8") == "# user edit\n"
    err = capsys.readouterr().err
    assert "exists" in err
    assert "--force" in err


def test_install_force_overwrites_existing_helper(tmp_path: Path, project: Path) -> None:
    repo = _make_registry(tmp_path)
    (project / "helpers").mkdir()
    existing = project / "helpers" / "echo.py"
    existing.write_text("# user edit\n", encoding="utf-8")

    rc = registry.install("echo", project, f"file://{repo}", force=True)

    assert rc == 0
    assert "echo_get" in existing.read_text(encoding="utf-8")


def test_install_returns_1_when_clone_fails(
    project: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bogus = tmp_path / "does-not-exist"

    rc = registry.install("echo", project, f"file://{bogus}")

    assert rc == 1
    err = capsys.readouterr().err
    assert "clone failed" in err


def test_install_announces_auth_when_service_requires_it(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    services = {
        "echo": {
            "version": "0.1.0",
            "path": "helpers/echo/echo.py",
            "description": "x",
            "summary_for_index": "x",
            "auth_required": True,
            "tags": [],
        }
    }
    repo = _make_registry(tmp_path, services=services)

    rc = registry.install("echo", project, f"file://{repo}")

    assert rc == 0
    err = capsys.readouterr().err
    assert "auth.toml" in err
    assert "echo" in err


def test_install_cleans_up_tempdir(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regardless of success, no leftover temp clone should remain in TMPDIR."""
    repo = _make_registry(tmp_path)
    isolated_tmp = tmp_path / "tmp"
    isolated_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(isolated_tmp))

    assert registry.install("echo", project, f"file://{repo}") == 0

    leftovers = [p for p in isolated_tmp.iterdir() if p.name.startswith("graft-registry-")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# url resolution: --registry > env > default
# ---------------------------------------------------------------------------


def test_install_uses_env_when_url_is_none(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_registry(tmp_path)
    monkeypatch.setenv("GRAFT_REGISTRY_URL", f"file://{repo}")

    rc = registry.install("echo", project, None)

    assert rc == 0
    assert (project / "helpers" / "echo.py").exists()


def test_install_falls_back_to_default_when_env_unset(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When url=None and no env, install attempts DEFAULT_REGISTRY_URL.

    We don't hit the network; we just assert the resolution touches the default
    by patching the default to our local fixture.
    """
    repo = _make_registry(tmp_path)
    monkeypatch.delenv("GRAFT_REGISTRY_URL", raising=False)
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY_URL", f"file://{repo}")

    rc = registry.install("echo", project, None)

    assert rc == 0
    assert (project / "helpers" / "echo.py").exists()


def test_install_explicit_url_beats_env(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit url must win over env even when env points elsewhere."""
    primary = _make_registry(tmp_path)
    other = tmp_path / "decoy"
    other.mkdir()
    monkeypatch.setenv("GRAFT_REGISTRY_URL", f"file://{other}")

    rc = registry.install("echo", project, f"file://{primary}")

    assert rc == 0
    assert (project / "helpers" / "echo.py").exists()


# ---------------------------------------------------------------------------
# safety contract: service name + manifest path must not escape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "service",
    ["../evil", "../../etc/passwd", "foo/bar", "_hidden", "1bad", "with space", ""],
)
def test_install_rejects_unsafe_service_name(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str], service: str
) -> None:
    repo = _make_registry(tmp_path)

    rc = registry.install(service, project, f"file://{repo}")

    assert rc == 1
    assert "invalid service name" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad_path",
    ["../../etc/x.py", "/etc/x.py", "helpers/../../etc/x.py", "../sibling.py"],
)
def test_install_rejects_manifest_path_escaping_registry(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str], bad_path: str
) -> None:
    services = {
        "echo": {
            "version": "0.1.0",
            "path": bad_path,
            "description": "x",
            "summary_for_index": "x",
            "auth_required": False,
            "tags": [],
        }
    }
    repo = _make_registry(tmp_path, services=services, write_helper=False)
    (repo / "placeholder").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "placeholder")

    rc = registry.install("echo", project, f"file://{repo}")

    assert rc == 1
    err = capsys.readouterr().err
    assert "manifest path escapes registry" in err
    assert not (project / "helpers" / "echo.py").exists()


# ---------------------------------------------------------------------------
# manifest invalid: malformed json / missing keys are surfaced cleanly
# ---------------------------------------------------------------------------


def _commit_raw_manifest(repo: Path, raw: str) -> None:
    (repo / "manifest.json").write_text(raw, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rewrite manifest")


def test_install_returns_1_on_malformed_manifest_json(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry(tmp_path)
    _commit_raw_manifest(repo, "{not valid json")

    rc = registry.install("echo", project, f"file://{repo}")

    assert rc == 1
    assert "manifest invalid" in capsys.readouterr().err


def test_install_returns_1_when_manifest_missing_services_key(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry(tmp_path)
    _commit_raw_manifest(repo, json.dumps({"$schema_version": "1"}))

    rc = registry.install("echo", project, f"file://{repo}")

    assert rc == 1
    assert "manifest invalid" in capsys.readouterr().err


def test_install_returns_1_when_service_entry_missing_path(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry(tmp_path)
    _commit_raw_manifest(
        repo,
        json.dumps({"$schema_version": "1", "services": {"echo": {"version": "0.1.0"}}}),
    )

    rc = registry.install("echo", project, f"file://{repo}")

    assert rc == 1
    assert "manifest invalid" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# dogfood: hit a local graft-registry checkout (set GRAFT_REGISTRY_DIR)
# ---------------------------------------------------------------------------


_DOGFOOD = Path(env) if (env := os.environ.get("GRAFT_REGISTRY_DIR")) else None


@pytest.mark.skipif(
    _DOGFOOD is None or not (_DOGFOOD / "manifest.json").exists(),
    reason="GRAFT_REGISTRY_DIR not set or registry missing",
)
def test_install_dogfood_local_registry_echo(project: Path) -> None:
    assert _DOGFOOD is not None
    rc = registry.install("echo", project, f"file://{_DOGFOOD}")

    assert rc == 0
    written = project / "helpers" / "echo.py"
    assert written.exists()
    source_path = _DOGFOOD / "helpers" / "echo" / "echo.py"
    assert written.read_text(encoding="utf-8") == source_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# security: git argument injection
# ---------------------------------------------------------------------------


def test_install_rejects_url_starting_with_dash(
    project: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """URL beginning with `-` must not be parsed by git as an option flag."""
    canary = tmp_path / "graft-injection-pwned"
    rc = registry.install("echo", project, f"--upload-pack=touch {canary}")

    assert rc == 1
    assert "registry clone failed" in capsys.readouterr().err
    assert not canary.exists()
