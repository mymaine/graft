"""Unit tests for graft.skill — render_skill_md + generate_index."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pytest import CaptureFixture

from graft.skill import (
    PROJECT_SKILL_PATH,
    generate_index,
    render_skill_md,
    write_project_skill,
)


def _write_helper(helpers_dir: Path, name: str, docstring: str | None) -> None:
    helpers_dir.mkdir(parents=True, exist_ok=True)
    body = f'"""{docstring}"""\n' if docstring is not None else ""
    body += "def ping() -> None:\n    return None\n"
    (helpers_dir / name).write_text(body, encoding="utf-8")


def _write_stats(stats_path: Path, lines: list[dict[str, object]]) -> None:
    import json

    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with stats_path.open("a", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _stat_record(service: str, helper: str, ts: datetime, ok: bool = True) -> dict[str, object]:
    return {
        "service": service,
        "helper": helper,
        "ts": ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": ok,
    }


# ---------------- render_skill_md ----------------


def test_render_skill_md_substitutes_version() -> None:
    template = "# Graft v{{VERSION}}\nbody\n"
    assert render_skill_md(template, "0.1.0") == "# Graft v0.1.0\nbody\n"


def test_render_skill_md_no_placeholder_returns_template_unchanged() -> None:
    template = "# Graft\nno placeholder here\n"
    assert render_skill_md(template, "9.9.9") == template


def test_render_skill_md_replaces_all_occurrences() -> None:
    template = "v{{VERSION}} ... still v{{VERSION}}"
    assert render_skill_md(template, "1.2.3") == "v1.2.3 ... still v1.2.3"


# ---------------- generate_index ----------------

HEADER = "# graft helpers index (auto-generated)\n\n"


def test_generate_index_empty_helpers_dir_returns_header_only(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"  # does not exist
    stats_path = tmp_path / ".graft" / "stats.jsonl"
    assert generate_index(helpers_dir, stats_path) == HEADER


def test_generate_index_empty_existing_dir_returns_header_only(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"
    helpers_dir.mkdir()
    stats_path = tmp_path / ".graft" / "stats.jsonl"
    assert generate_index(helpers_dir, stats_path) == HEADER


def test_generate_index_single_service_with_docstring_and_stats(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"
    _write_helper(helpers_dir, "github.py", "GitHub REST + GraphQL\nlong description")
    stats_path = tmp_path / ".graft" / "stats.jsonl"
    ts = datetime(2026, 4, 26, 15, 30, 0, tzinfo=UTC)
    records = [_stat_record("github", f"helper_{i}", ts) for i in range(12)]
    records.extend(_stat_record("github", "helper_0", ts) for _ in range(8))
    _write_stats(stats_path, records)

    out = generate_index(helpers_dir, stats_path)
    assert out.startswith(HEADER)
    assert "- github.py (12 helpers, 20 calls, last: 2026-04-26): GitHub REST + GraphQL\n" in out
    assert out.endswith("\n")


def test_generate_index_sorts_by_total_calls_desc(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"
    _write_helper(helpers_dir, "alpha.py", "alpha service")
    _write_helper(helpers_dir, "bravo.py", "bravo service")
    _write_helper(helpers_dir, "charlie.py", "charlie service")
    stats_path = tmp_path / ".graft" / "stats.jsonl"
    ts = datetime(2026, 4, 26, 0, 0, 0, tzinfo=UTC)
    records: list[dict[str, object]] = []
    for _ in range(50):
        records.append(_stat_record("alpha", "h", ts))
    for _ in range(200):
        records.append(_stat_record("bravo", "h", ts))
    for _ in range(100):
        records.append(_stat_record("charlie", "h", ts))
    _write_stats(stats_path, records)

    out = generate_index(helpers_dir, stats_path)
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(lines) == 3
    assert lines[0].startswith("- bravo.py")
    assert lines[1].startswith("- charlie.py")
    assert lines[2].startswith("- alpha.py")


def test_generate_index_service_without_stats_uses_zero_and_never(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"
    _write_helper(helpers_dir, "notion.py", "Notion pages, databases")
    stats_path = tmp_path / ".graft" / "stats.jsonl"  # not created

    out = generate_index(helpers_dir, stats_path)
    assert "- notion.py (0 helpers, 0 calls, last: never): Notion pages, databases\n" in out


def test_generate_index_excludes_init_and_underscore_files(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"
    _write_helper(helpers_dir, "__init__.py", "package init")
    _write_helper(helpers_dir, "_internal.py", "internal helper")
    _write_helper(helpers_dir, "github.py", "GitHub REST + GraphQL")
    stats_path = tmp_path / ".graft" / "stats.jsonl"

    out = generate_index(helpers_dir, stats_path)
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(lines) == 1
    assert lines[0].startswith("- github.py")


def test_generate_index_missing_module_docstring_yields_empty_description(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"
    _write_helper(helpers_dir, "foo.py", None)
    stats_path = tmp_path / ".graft" / "stats.jsonl"

    out = generate_index(helpers_dir, stats_path)
    assert "- foo.py (0 helpers, 0 calls, last: never): \n" in out


def test_generate_index_zero_call_services_alpha_sorted(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"
    _write_helper(helpers_dir, "zeta.py", "zeta")
    _write_helper(helpers_dir, "alpha.py", "alpha")
    _write_helper(helpers_dir, "mid.py", "mid")
    stats_path = tmp_path / ".graft" / "stats.jsonl"
    ts = datetime(2026, 4, 26, 0, 0, 0, tzinfo=UTC)
    _write_stats(stats_path, [_stat_record("mid", "h", ts) for _ in range(5)])

    out = generate_index(helpers_dir, stats_path)
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert lines[0].startswith("- mid.py")
    assert lines[1].startswith("- alpha.py")
    assert lines[2].startswith("- zeta.py")


def test_generate_index_uses_first_line_of_docstring_only(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"
    _write_helper(helpers_dir, "linear.py", "Linear issues, projects\nMore details on second line")
    stats_path = tmp_path / ".graft" / "stats.jsonl"

    out = generate_index(helpers_dir, stats_path)
    assert "Linear issues, projects" in out
    assert "More details on second line" not in out


def test_generate_index_warns_on_syntax_error_in_helper(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    helpers_dir = tmp_path / "helpers"
    helpers_dir.mkdir()
    (helpers_dir / "broken.py").write_text("def foo(:\n    pass\n", encoding="utf-8")
    stats_path = tmp_path / ".graft" / "stats.jsonl"

    out = generate_index(helpers_dir, stats_path)

    err = capsys.readouterr().err
    assert "syntax error" in err
    assert "broken.py" in err
    assert "broken.py" in out


def test_generate_index_extracts_summary_from_pep257_multiline_docstring(tmp_path: Path) -> None:
    helpers_dir = tmp_path / "helpers"
    helpers_dir.mkdir()
    (helpers_dir / "foo.py").write_text(
        '"""\nMulti-line summary.\n\nLonger body description.\n"""\n', encoding="utf-8"
    )
    stats_path = tmp_path / ".graft" / "stats.jsonl"

    out = generate_index(helpers_dir, stats_path)
    assert "Multi-line summary" in out
    assert "Longer body description" not in out


def test_write_project_skill_creates_nested_dirs(tmp_path: Path) -> None:
    write_project_skill(tmp_path, "skill body\n")
    target = tmp_path / PROJECT_SKILL_PATH
    assert target.read_text(encoding="utf-8") == "skill body\n"
    assert target.parent.is_dir()
