"""JSONL stats append + aggregate. One line per call, ≤ 1024B for POSIX-atomic append."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(".graft/stats.jsonl")
MAX_LINE_BYTES = 1024
ELLIPSIS = "…"


@dataclass(frozen=True)
class ServiceStats:
    helper_count: int
    total_calls: int
    last_ts: datetime
    errors: int


@dataclass(frozen=True)
class HelperStats:
    calls: int
    errors: int
    last_ts: datetime


def _dump(service: str, helper: str, iso_ts: str, ok: bool) -> bytes:
    payload = {"service": service, "helper": helper, "ts": iso_ts, "ok": ok}
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def append(service: str, helper: str, ts: datetime, ok: bool, path: Path = DEFAULT_PATH) -> None:
    """Best-effort: never raises; IO failures warn to stderr and silently drop the record."""
    iso = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = _dump(service, helper, iso, ok)
    if len(line) > MAX_LINE_BYTES:
        budget = MAX_LINE_BYTES - len(_dump(service, "", iso, ok)) - len(ELLIPSIS.encode("utf-8"))
        if budget < 0:
            print(f"warning: stats: service name too long ({len(service)}B)", file=sys.stderr)
            return
        clipped = helper.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        line = _dump(service, clipped + ELLIPSIS, iso, ok)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as f:
            f.write(line)
    except OSError as e:
        print(f"warning: stats append failed: {e}", file=sys.stderr)


def aggregate(path: Path) -> dict[str, ServiceStats]:
    """Reduce stats.jsonl. Missing/empty file => {}; corrupt line => skip + stderr warn."""
    acc: dict[str, dict[str, Any]] = {}
    for (svc, helper), h in aggregate_helpers(path).items():
        b = acc.setdefault(svc, {"helpers": set(), "calls": 0, "errors": 0, "last_ts": h.last_ts})
        b["helpers"].add(helper)
        b["calls"] += h.calls
        b["errors"] += h.errors
        if h.last_ts > b["last_ts"]:
            b["last_ts"] = h.last_ts
    return {
        s: ServiceStats(len(b["helpers"]), b["calls"], b["last_ts"], b["errors"])
        for s, b in acc.items()
    }


def aggregate_helpers(path: Path) -> dict[tuple[str, str], HelperStats]:
    """Per-helper aggregate keyed by (service, helper). Same parse rules as aggregate()."""
    if not path.exists():
        return {}
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        rec = _parse(raw, n)
        if rec is None:
            continue
        b = acc.setdefault(
            (rec["service"], rec["helper"]), {"calls": 0, "errors": 0, "last_ts": None}
        )
        b["calls"] += 1
        b["errors"] += 0 if rec["ok"] else 1
        if b["last_ts"] is None or rec["ts"] > b["last_ts"]:
            b["last_ts"] = rec["ts"]
    return {k: HelperStats(b["calls"], b["errors"], b["last_ts"]) for k, b in acc.items()}


def _parse(raw: str, n: int) -> dict[str, Any] | None:
    def warn(reason: str) -> None:
        print(f"warning: stats.jsonl line {n} corrupt: {reason}", file=sys.stderr)

    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        warn(e.msg)
        return None
    if not (
        isinstance(rec, dict)
        and all(isinstance(rec.get(k), str) for k in ("service", "helper", "ts"))
        and isinstance(rec.get("ok"), bool)
    ):
        warn("bad fields")
        return None
    try:
        rec["ts"] = datetime.fromisoformat(rec["ts"])
    except ValueError as e:
        warn(str(e))
        return None
    return rec
