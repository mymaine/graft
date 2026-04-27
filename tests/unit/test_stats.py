"""Unit tests for graft.stats.

Tests must run before src/graft/stats.py exists (red phase of TDD).
"""

from __future__ import annotations

import json
import multiprocessing as mp
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest import CaptureFixture

from graft.stats import HelperStats, ServiceStats, aggregate, aggregate_helpers, append

FIXED_TS = datetime(2026, 4, 26, 15, 30, 21, tzinfo=UTC)


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_append_writes_well_formed_json_line(tmp_path: Path) -> None:
    target = tmp_path / "stats.jsonl"
    append("github", "list_issues", FIXED_TS, ok=True, path=target)

    raw_bytes = target.read_bytes()
    assert raw_bytes.endswith(b"\n"), "every line must end with \\n"

    lines = _read_lines(target)
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed == {
        "service": "github",
        "helper": "list_issues",
        "ts": "2026-04-26T15:30:21Z",
        "ok": True,
    }


def test_append_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / ".graft" / "stats.jsonl"
    assert not target.parent.exists()

    append("github", "get_repo", FIXED_TS, ok=True, path=target)

    assert target.parent.is_dir()
    assert target.exists()


def test_append_truncates_oversized_helper_to_1024_byte_limit(tmp_path: Path) -> None:
    target = tmp_path / "stats.jsonl"
    huge_name = "a" * 2000

    append("github", huge_name, FIXED_TS, ok=True, path=target)

    raw = target.read_bytes()
    assert raw.endswith(b"\n")
    assert len(raw) <= 1024, f"line including newline must be <= 1024 bytes, got {len(raw)}"

    payload = json.loads(raw.decode("utf-8").rstrip("\n"))
    assert payload["service"] == "github"
    assert payload["ok"] is True
    assert payload["ts"] == "2026-04-26T15:30:21Z"
    assert payload["helper"].endswith("…"), "truncated helper must carry ellipsis marker"
    assert payload["helper"].startswith("a")


def test_aggregate_skips_corrupt_lines_and_warns(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    target = tmp_path / "stats.jsonl"
    good_one = json.dumps(
        {"service": "github", "helper": "list_issues", "ts": "2026-04-26T15:30:21Z", "ok": True}
    )
    good_two = json.dumps(
        {"service": "github", "helper": "get_repo", "ts": "2026-04-26T15:30:25Z", "ok": True}
    )
    target.write_text(f"{good_one}\nnot json\n{good_two}\n", encoding="utf-8")

    result = aggregate(target)

    captured = capsys.readouterr()
    assert "stats.jsonl line 2 corrupt" in captured.err
    assert "github" in result
    stats = result["github"]
    assert stats.total_calls == 2
    assert stats.errors == 0
    assert stats.helper_count == 2


def test_aggregate_empty_file_returns_empty_dict(tmp_path: Path) -> None:
    target = tmp_path / "stats.jsonl"
    target.write_text("", encoding="utf-8")

    assert aggregate(target) == {}


def test_aggregate_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    target = tmp_path / "does-not-exist.jsonl"

    assert aggregate(target) == {}


def test_aggregate_groups_multiple_services(tmp_path: Path) -> None:
    target = tmp_path / "stats.jsonl"
    rows = [
        ("github", "list_issues", datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC), True),
        ("github", "list_issues", datetime(2026, 4, 26, 10, 1, 0, tzinfo=UTC), False),
        ("github", "get_repo", datetime(2026, 4, 26, 10, 2, 0, tzinfo=UTC), True),
        ("linear", "list_issues", datetime(2026, 4, 26, 11, 0, 0, tzinfo=UTC), True),
        ("linear", "create_issue", datetime(2026, 4, 26, 11, 5, 0, tzinfo=UTC), True),
    ]
    for service, helper, ts, ok in rows:
        append(service, helper, ts, ok=ok, path=target)

    result = aggregate(target)

    assert list(result.keys()) == ["github", "linear"], "preserves insertion order"

    github = result["github"]
    assert isinstance(github, ServiceStats)
    assert github.total_calls == 3
    assert github.errors == 1
    assert github.helper_count == 2
    assert github.last_ts == datetime(2026, 4, 26, 10, 2, 0, tzinfo=UTC)

    linear = result["linear"]
    assert linear.total_calls == 2
    assert linear.errors == 0
    assert linear.helper_count == 2
    assert linear.last_ts == datetime(2026, 4, 26, 11, 5, 0, tzinfo=UTC)


def _worker(path_str: str, worker_id: int) -> None:
    from datetime import UTC, datetime

    from graft.stats import append

    target = Path(path_str)
    for i in range(5):
        ts = datetime(2026, 4, 26, 12, worker_id, i, tzinfo=UTC)
        append("svc", f"helper_{worker_id}_{i}", ts, ok=True, path=target)


def test_concurrent_append_produces_intact_lines(tmp_path: Path) -> None:
    target = tmp_path / "stats.jsonl"
    ctx = mp.get_context("spawn")
    workers = [ctx.Process(target=_worker, args=(str(target), i)) for i in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
        assert w.exitcode == 0

    lines = _read_lines(target)
    assert len(lines) == 20
    for line in lines:
        parsed = json.loads(line)
        assert parsed["service"] == "svc"
        assert parsed["ok"] is True
        assert parsed["helper"].startswith("helper_")


def test_aggregate_counts_errors(tmp_path: Path) -> None:
    target = tmp_path / "stats.jsonl"
    append("github", "list_issues", FIXED_TS, ok=False, path=target)
    append("github", "list_issues", FIXED_TS, ok=False, path=target)
    append("github", "list_issues", FIXED_TS, ok=True, path=target)

    result = aggregate(target)
    assert result["github"].errors == 2
    assert result["github"].total_calls == 3
    assert result["github"].helper_count == 1


def test_aggregate_skips_lines_with_wrong_field_types(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    target = tmp_path / "stats.jsonl"
    bad = json.dumps({"service": 1, "helper": "x", "ts": "2026-04-26T15:30:21Z", "ok": True})
    missing = json.dumps({"service": "github", "helper": "x", "ok": True})
    good = json.dumps(
        {"service": "github", "helper": "x", "ts": "2026-04-26T15:30:21Z", "ok": True}
    )
    target.write_text(f"{bad}\n{missing}\n{good}\n", encoding="utf-8")

    result = aggregate(target)

    captured = capsys.readouterr()
    assert "line 1 corrupt" in captured.err
    assert "line 2 corrupt" in captured.err
    assert result["github"].total_calls == 1


def test_aggregate_skips_lines_with_invalid_ts_and_warns(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    target = tmp_path / "stats.jsonl"
    bad = json.dumps({"service": "github", "helper": "x", "ts": "not-a-date", "ok": True})
    good = json.dumps(
        {"service": "github", "helper": "x", "ts": "2026-04-26T15:30:21Z", "ok": True}
    )
    target.write_text(f"{bad}\n{good}\n", encoding="utf-8")

    result = aggregate(target)

    captured = capsys.readouterr()
    assert "line 1 corrupt" in captured.err
    assert result["github"].total_calls == 1
    assert result["github"].helper_count == 1


def test_append_swallows_oserror_and_warns(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "stats.jsonl"

    def boom(self: Path, *args: object, **kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", boom)

    append("github", "list_issues", FIXED_TS, ok=True, path=target)

    captured = capsys.readouterr()
    assert "stats append failed" in captured.err
    assert "disk full" in captured.err


def test_aggregate_helpers_missing_file_returns_empty(tmp_path: Path) -> None:
    assert aggregate_helpers(tmp_path / "none.jsonl") == {}


def test_aggregate_helpers_empty_file_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / "stats.jsonl"
    target.write_text("", encoding="utf-8")
    assert aggregate_helpers(target) == {}


def test_aggregate_helpers_skips_corrupt_and_warns(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    target = tmp_path / "stats.jsonl"
    good = json.dumps(
        {"service": "github", "helper": "list_issues", "ts": "2026-04-26T15:30:21Z", "ok": True}
    )
    target.write_text(f"{good}\nnot json\n{good}\n", encoding="utf-8")

    result = aggregate_helpers(target)

    assert "stats.jsonl line 2 corrupt" in capsys.readouterr().err
    key = ("github", "list_issues")
    assert isinstance(result[key], HelperStats)
    assert result[key].calls == 2
    assert result[key].errors == 0


def test_aggregate_helpers_groups_per_helper_across_services(tmp_path: Path) -> None:
    target = tmp_path / "stats.jsonl"
    rows = [
        ("github", "list_issues", datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC), True),
        ("github", "list_issues", datetime(2026, 4, 26, 10, 1, 0, tzinfo=UTC), False),
        ("github", "get_repo", datetime(2026, 4, 26, 10, 2, 0, tzinfo=UTC), True),
        ("linear", "list_issues", datetime(2026, 4, 25, 9, 0, 0, tzinfo=UTC), True),
    ]
    for service, helper, ts, ok in rows:
        append(service, helper, ts, ok=ok, path=target)

    result = aggregate_helpers(target)

    gh_list = result[("github", "list_issues")]
    assert gh_list.calls == 2
    assert gh_list.errors == 1
    assert gh_list.last_ts == datetime(2026, 4, 26, 10, 1, 0, tzinfo=UTC)

    gh_get = result[("github", "get_repo")]
    assert gh_get.calls == 1
    assert gh_get.errors == 0

    linear_list = result[("linear", "list_issues")]
    assert linear_list.calls == 1
    assert linear_list.errors == 0
    assert ("github", "list_issues") in result and ("linear", "list_issues") in result


def test_append_skips_when_service_name_too_long(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    target = tmp_path / "stats.jsonl"

    append("a" * 2000, "helper", FIXED_TS, ok=True, path=target)

    captured = capsys.readouterr()
    assert "service name too long" in captured.err
    assert not target.exists() or target.read_bytes() == b""
