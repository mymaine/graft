"""graft CLI: init / sync / stats / serve / reset."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from importlib import resources
from importlib.metadata import version
from pathlib import Path

import httpx

from graft import daemon, loader, skill, stats

VERSION = version("graft")


def _read_template() -> str:
    """Read templates/SKILL.md from graft package data."""
    return (resources.files("graft") / "templates" / "SKILL.md").read_text(encoding="utf-8")


def _read_helpers_init() -> str:
    """Read templates/helpers__init__.py.tmpl from graft package data."""
    path = resources.files("graft") / "templates" / "helpers__init__.py.tmpl"
    return path.read_text(encoding="utf-8")


def _render_template() -> str:
    return skill.render_skill_md(_read_template(), VERSION)


def init(_args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    (cwd / "helpers").mkdir(exist_ok=True)
    (cwd / "helpers" / "__init__.py").write_text(_read_helpers_init(), encoding="utf-8")
    (cwd / ".graft").mkdir(exist_ok=True)
    (cwd / "SKILL.md").write_text(_render_template(), encoding="utf-8")
    print("graft initialized: helpers/, .graft/, SKILL.md", file=sys.stderr)
    return 0


def sync(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    helpers_dir = cwd / "helpers"
    helpers_dir.mkdir(exist_ok=True)
    index = skill.generate_index(helpers_dir, cwd / ".graft" / "stats.jsonl")
    (helpers_dir / "INDEX.md").write_text(index, encoding="utf-8")

    target = cwd / "SKILL.md"
    rendered = _render_template()
    if not target.exists():
        target.write_text(rendered, encoding="utf-8")
    elif target.read_text(encoding="utf-8").replace("\r\n", "\n") != rendered:
        if args.force:
            target.write_text(rendered, encoding="utf-8")
            print("SKILL.md updated (--force)", file=sys.stderr)
        else:
            print(
                "SKILL.md differs from installed template; run `graft sync --force` to overwrite",
                file=sys.stderr,
            )
    print("graft sync: helpers/INDEX.md regenerated", file=sys.stderr)
    return 0


def stats_cmd(_args: argparse.Namespace) -> int:
    agg = stats.aggregate(Path.cwd() / ".graft" / "stats.jsonl")
    if not agg:
        print("(no stats yet)", file=sys.stderr)
        return 0
    print(f"{'service':<12} {'helpers':>8} {'calls':>8} {'errors':>8} {'last':>12}")
    for name, s in sorted(agg.items(), key=lambda kv: -kv[1].total_calls):
        last = s.last_ts.strftime("%Y-%m-%d")
        print(f"{name:<12} {s.helper_count:>8} {s.total_calls:>8} {s.errors:>8} {last:>12}")
    return 0


def serve(_args: argparse.Namespace) -> int:
    daemon.serve()
    return 0


def reset(args: argparse.Namespace) -> int:
    port_file = loader.PORT_FILE
    if not port_file.exists():
        print("daemon not running; start it with `graft serve`", file=sys.stderr)
        return 1
    _, _, port_str = port_file.read_text(encoding="utf-8").strip().partition(":")
    try:
        port = int(port_str)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as c:
            r = c.post("/circuit/reset", json={"service": args.service})
            r.raise_for_status()
    except (httpx.HTTPError, ValueError) as e:
        print(f"reset failed: {e}", file=sys.stderr)
        return 1
    print(f"circuit reset for service '{args.service}'", file=sys.stderr)
    return 0


_DISPATCH: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": init,
    "sync": sync,
    "stats": stats_cmd,
    "serve": serve,
    "reset": reset,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graft")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("sync").add_argument("--force", action="store_true")
    sub.add_parser("stats")
    sub.add_parser("serve")
    reset_p = sub.add_parser("reset")
    reset_p.add_argument("service")
    args = parser.parse_args(argv)
    return _DISPATCH[args.cmd](args)
