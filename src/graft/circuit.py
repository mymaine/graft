"""In-memory failure counter. Lives in daemon; resets on daemon restart."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CircuitAction:
    count: int
    action: str


class Circuit:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def record_failure(self, service: str) -> CircuitAction:
        n = self._counts[service] = self._counts.get(service, 0) + 1
        a = "abort" if n >= 5 else "raise_with_template" if n >= 3 else "raise"
        return CircuitAction(n, a)

    def record_success(self, service: str) -> None:
        self._counts.pop(service, None)

    def reset(self, service: str) -> None:
        self._counts.pop(service, None)
