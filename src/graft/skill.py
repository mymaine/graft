"""SKILL.md / INDEX.md template + auto-generation."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from graft.stats import ServiceStats, aggregate

VERSION_PLACEHOLDER = "{{VERSION}}"
INDEX_HEADER = "# graft helpers index (auto-generated)\n\n"
PROJECT_SKILL_PATH = Path(".claude/skills/graft/SKILL.md")
CLAUDE_MD_NOTE = (
    "\nThis project uses graft. See `.claude/skills/graft/SKILL.md` for helper conventions.\n"
)


def render_skill_md(template: str, version: str) -> str:
    """Substitute {{VERSION}} placeholder in SKILL.md template."""
    return template.replace(VERSION_PLACEHOLDER, version)


def write_project_skill(cwd: Path, content: str) -> None:
    """Write SKILL.md to .claude/skills/graft/ so Claude Code auto-discovers it."""
    target = cwd / PROJECT_SKILL_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def append_claude_md(cwd: Path) -> None:
    """Append a graft pointer to project CLAUDE.md (idempotent — skip if 'graft' present)."""
    cm = cwd / "CLAUDE.md"
    old = cm.read_text(encoding="utf-8") if cm.exists() else ""
    if "graft" in old:
        return
    cm.write_text(old + CLAUDE_MD_NOTE, encoding="utf-8")


def generate_index(helpers_dir: Path, stats_path: Path) -> str:
    """Build INDEX.md by joining helpers/*.py with stats.aggregate (UTC dates)."""
    if not helpers_dir.exists():
        return INDEX_HEADER
    services = [
        (p.stem, p.name, _module_summary(p))
        for p in sorted(helpers_dir.glob("*.py"))
        if p.name != "__init__.py" and not p.name.startswith("_")
    ]
    stats = aggregate(stats_path)
    rows = [_format_row(name, fname, desc, stats.get(name)) for name, fname, desc in services]
    rows.sort(key=lambda r: (-r[0], r[1]))
    return INDEX_HEADER + "".join(line for _, _, line in rows)


def _module_summary(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        print(f"warning: skill: {path.name} has syntax error: {e.msg}", file=sys.stderr)
        return ""
    doc = ast.get_docstring(tree)
    if not doc:
        return ""
    return doc.split("\n", 1)[0].strip()


def _format_row(
    service: str, filename: str, description: str, stat: ServiceStats | None
) -> tuple[int, str, str]:
    if stat is None:
        helpers, calls, last = 0, 0, "never"
    else:
        helpers, calls = stat.helper_count, stat.total_calls
        last = stat.last_ts.strftime("%Y-%m-%d")
    line = f"- {filename} ({helpers} helpers, {calls} calls, last: {last}): {description}\n"
    return calls, service, line
