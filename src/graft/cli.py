"""graft CLI: init / sync / stats / hot / serve / reset / inspect / prune / add."""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from importlib.metadata import version
from pathlib import Path

import httpx

from graft import daemon, git_memory, loader, registry, skill, stats
from graft.stats import HelperStats

VERSION = version("graft")


def _read_pkg(name: str) -> str:
    return (resources.files("graft") / "templates" / name).read_text(encoding="utf-8")


def _render_template() -> str:
    return skill.render_skill_md(_read_pkg("SKILL.md"), VERSION)


def init(_args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    (cwd / "helpers").mkdir(exist_ok=True)
    (cwd / "helpers" / "__init__.py").write_text(_read_pkg("init.tmpl"), encoding="utf-8")
    (cwd / ".graft").mkdir(exist_ok=True)
    skill.write_project_skill(cwd, _render_template())
    skill.append_claude_md(cwd)
    gi = cwd / ".gitignore"
    old = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    have = set(map(str.strip, old))
    if missing := [e for e in (".graft/", "__pycache__/", "*.pyc") if e not in have]:
        gi.write_text("\n".join(old + missing) + "\n", encoding="utf-8")
    print("graft initialized: helpers/, .graft/, .claude/skills/graft/, CLAUDE.md", file=sys.stderr)
    return 0


def sync(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    helpers_dir = cwd / "helpers"
    helpers_dir.mkdir(exist_ok=True)
    index = skill.generate_index(helpers_dir, cwd / ".graft" / "stats.jsonl")
    (helpers_dir / "INDEX.md").write_text(index, encoding="utf-8")

    target = cwd / skill.PROJECT_SKILL_PATH
    rendered = _render_template()
    if not target.exists():
        skill.write_project_skill(cwd, rendered)
    elif target.read_text(encoding="utf-8").replace("\r\n", "\n") != rendered:
        if args.force:
            skill.write_project_skill(cwd, rendered)
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


def hot_cmd(args: argparse.Namespace) -> int:
    agg = stats.aggregate_helpers(Path.cwd() / ".graft" / "stats.jsonl")
    if not agg:
        print("(no stats yet)", file=sys.stderr)
        return 0
    ranked = sorted(agg.items(), key=lambda kv: (-kv[1].calls, -kv[1].errors))[: max(0, args.limit)]
    print(f"{'service':<12} {'helper':<24} {'calls':>8} {'errors':>8} {'last':>12}")
    for (svc, helper), s in ranked:
        last = s.last_ts.strftime("%Y-%m-%d")
        print(f"{svc:<12} {helper:<24} {s.calls:>8} {s.errors:>8} {last:>12}")
    return 0


def serve(_args: argparse.Namespace) -> int:
    print("graft daemon starting (Ctrl+C to stop, port in .graft/daemon.port)", file=sys.stderr)
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


def _public_helpers_in(path: Path) -> list[str]:
    """Static ast walk: top-level public def names. Avoids importing helper modules."""
    return [
        n.name
        for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith("_")
    ]


def inspect_cmd(args: argparse.Namespace) -> int:
    src = Path.cwd() / "helpers" / f"{args.service}.py"
    if not src.exists():
        print(f"helper file not found: helpers/{args.service}.py", file=sys.stderr)
        return 1
    try:
        names = _public_helpers_in(src)
    except (OSError, SyntaxError) as e:
        print(f"failed to parse helpers/{args.service}.py: {e}", file=sys.stderr)
        return 1
    if not names:
        print(f"(no public helpers in {args.service}.py)", file=sys.stderr)
        return 0
    agg = stats.aggregate_helpers(Path.cwd() / ".graft" / "stats.jsonl")
    rows: list[tuple[str, HelperStats | None]] = [(n, agg.get((args.service, n))) for n in names]
    rows.sort(key=lambda r: (1, r[0]) if r[1] is None else (0, -r[1].calls, -r[1].errors))
    calls = sum(s.calls for _, s in rows if s)
    errs = sum(s.errors for _, s in rows if s)
    print(f"service: {args.service}     ({len(names)} helpers, {calls} calls, {errs} errors)\n")
    print(f"{'helper':<28} {'calls':>8} {'errors':>8} {'last':>12}")
    for name, s in rows:
        c, er, last = (s.calls, s.errors, s.last_ts.strftime("%Y-%m-%d")) if s else (0, 0, "N/A")
        print(f"{name:<28} {c:>8} {er:>8} {last:>12}")
    return 0


def prune_cmd(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    helpers = cwd / "helpers"
    last_used: dict[str, datetime] = {}
    for (svc, _), h in stats.aggregate_helpers(cwd / ".graft" / "stats.jsonl").items():
        if h.last_ts > last_used.get(svc, datetime.min.replace(tzinfo=UTC)):
            last_used[svc] = h.last_ts
    now = datetime.now(UTC)
    plan: list[tuple[str, datetime, int]] = []
    for f in sorted(helpers.glob("*.py") if helpers.is_dir() else []):
        if f.name.startswith(("_", ".")):
            continue
        last = last_used.get(f.stem)
        if last is None:
            ts = git_memory.git_log_mtime(f"helpers/{f.stem}.py", cwd)
            if ts is None:
                continue
            last = datetime.fromtimestamp(ts, tz=UTC)
        plan.append((f.stem, last, (now - last).days))
    if not plan:
        print("(no helpers)", file=sys.stderr)
        return 0
    stale = [p for p in plan if p[2] > args.stale]
    if not args.apply:
        print(f"{'service':<12} {'last_used':<12} {'age (days)':>10}   action")
        for svc, last, age in plan:
            tag = "archive" if age > args.stale else "keep"
            print(f"{svc:<12} {last.strftime('%Y-%m-%d'):<12} {age:>10}   {tag}")
        return 0
    if not stale:
        print("(no stale helpers)", file=sys.stderr)
        return 0
    if reason := git_memory.is_dirty_outside_helpers(cwd):
        print(f"warning: prune paused: {reason}", file=sys.stderr)
        return 1
    autocommit = git_memory._autocommit_enabled()
    proceed = autocommit and git_memory.should_commit(cwd)[0]
    (helpers / "_archive").mkdir(exist_ok=True)
    for svc, _, age in stale:
        dst = helpers / "_archive" / f"{svc}.py"
        if dst.exists():
            print(f"warning: {dst} exists; overwriting", file=sys.stderr)
        if not git_memory.git_mv(helpers / f"{svc}.py", dst, cwd):
            continue
        print(f"archived: helpers/{svc}.py -> helpers/_archive/{svc}.py")
        if proceed and not git_memory.commit_helpers(
            f"chore: archive stale helper {svc} (untouched {age} days)", cwd
        ):
            print(f"warning: commit failed for {svc}; aborting", file=sys.stderr)
            return 1
    if not autocommit:
        print("GRAFT_AUTOCOMMIT=0: moves staged, commit manually.", file=sys.stderr)
    return 0


def add_cmd(args: argparse.Namespace) -> int:
    return registry.install(args.service, Path.cwd(), args.registry, force=args.force)


_DISPATCH: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": init,
    "sync": sync,
    "stats": stats_cmd,
    "hot": hot_cmd,
    "serve": serve,
    "reset": reset,
    "inspect": inspect_cmd,
    "prune": prune_cmd,
    "add": add_cmd,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graft")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("init", "stats", "serve"):
        sub.add_parser(cmd)
    sub.add_parser("sync").add_argument("--force", action="store_true")
    sub.add_parser("hot").add_argument("--limit", type=int, default=10)
    reset_p = sub.add_parser("reset")
    reset_p.add_argument("service")
    sub.add_parser("inspect").add_argument("service")
    prune_p = sub.add_parser("prune")
    prune_p.add_argument("--stale", type=int, default=90, help="Archive after N days (default 90)")
    prune_p.add_argument("--apply", action="store_true", help="Apply (default: dry-run)")
    add_p = sub.add_parser("add")
    add_p.add_argument("service")
    add_p.add_argument("--registry", help="Registry URL (file://, https://github.com/...)")
    add_p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    return _DISPATCH[args.cmd](args)
