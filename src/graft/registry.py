"""Registry client: shallow git clone -> manifest lookup -> validate -> copy.

v1 supports `file://...` and `https://github.com/...` URLs (anything `git clone`
accepts over those protocols). `ssh://` and `git@` are deferred to Phase 3+.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from graft import validator

DEFAULT_REGISTRY_URL = "https://github.com/mymaine/graft-registry"
ENV_VAR = "GRAFT_REGISTRY_URL"


def _fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


def install(
    service: str,
    project_dir: Path,
    registry_url: str | None,
    *,
    force: bool = False,
) -> int:
    """Pull <service> from registry into <project_dir>/helpers/<service>.py.

    URL resolution: explicit `registry_url` > `GRAFT_REGISTRY_URL` env >
    `DEFAULT_REGISTRY_URL`. Returns 0 on success, 1 on any failure (clone,
    manifest lookup, missing path, validator, dirty target). Stderr carries
    the human-readable reason; stdout announces success.
    """
    url = registry_url or os.environ.get(ENV_VAR) or DEFAULT_REGISTRY_URL
    if not service.isidentifier() or service.startswith("_"):
        return _fail(f"invalid service name: {service!r}")
    dst = project_dir / "helpers" / f"{service}.py"
    if dst.exists() and not force:
        return _fail(f"helpers/{service}.py exists; use --force to overwrite")
    tmp = Path(tempfile.mkdtemp(prefix="graft-registry-"))
    try:
        repo = tmp / "r"
        clone = subprocess.run(
            ["git", "clone", "--depth=1", "--", url, str(repo)], capture_output=True, text=True
        )
        if clone.returncode != 0:
            return _fail(f"registry clone failed: {clone.stderr.strip()}")
        try:
            services = json.loads((repo / "manifest.json").read_text(encoding="utf-8"))["services"]
            if (meta := services.get(service)) is None:
                return _fail(f"service {service!r} not in registry manifest")
            path_str = str(meta["path"])
            if path_str.startswith("/") or ".." in Path(path_str).parts:
                return _fail(f"manifest path escapes registry: {path_str!r}")
            src = repo / path_str
            if not src.exists():
                return _fail(f"registry path missing: {path_str}")
            source = src.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            return _fail(f"manifest invalid: {e}")
        if reason := validator.check(source, service):
            return _fail(f"registry helper failed validator: {reason}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(source, encoding="utf-8")
        print(f"added: helpers/{service}.py from {url}@{meta.get('version', '?')}")
        if meta.get("auth_required"):
            print(f".graft/auth.toml needs [{service}] section", file=sys.stderr)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
