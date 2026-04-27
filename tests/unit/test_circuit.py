"""Unit tests for graft.circuit.

Tests must run before src/graft/circuit.py exists (red phase of TDD).
Covers: 3/5 thresholds, success reset, multi-service isolation, abort absorbing
state, idempotent edges (success/reset on unknown service).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from graft.circuit import Circuit, CircuitAction


def test_first_failure_returns_count_one_action_raise() -> None:
    c = Circuit()

    result = c.record_failure("github")

    assert result == CircuitAction(count=1, action="raise")


def test_threshold_progression_raise_template_abort() -> None:
    c = Circuit()

    actions = [c.record_failure("github") for _ in range(6)]

    assert actions[0] == CircuitAction(count=1, action="raise")
    assert actions[1] == CircuitAction(count=2, action="raise")
    assert actions[2] == CircuitAction(count=3, action="raise_with_template")
    assert actions[3] == CircuitAction(count=4, action="raise_with_template")
    assert actions[4] == CircuitAction(count=5, action="abort")
    assert actions[5] == CircuitAction(count=6, action="abort")


def test_record_success_resets_counter() -> None:
    c = Circuit()
    c.record_failure("github")
    c.record_failure("github")

    c.record_success("github")
    after = c.record_failure("github")

    assert after == CircuitAction(count=1, action="raise")


def test_multi_service_isolation() -> None:
    c = Circuit()

    for _ in range(5):
        c.record_failure("github")
    linear = c.record_failure("linear")

    assert linear == CircuitAction(count=1, action="raise")
    # github stays in abort
    assert c.record_failure("github") == CircuitAction(count=6, action="abort")


def test_reset_clears_abort_state() -> None:
    c = Circuit()
    for _ in range(5):
        c.record_failure("github")

    c.reset("github")
    after = c.record_failure("github")

    assert after == CircuitAction(count=1, action="raise")


def test_idempotent_success_and_reset_on_unknown_service() -> None:
    c = Circuit()

    # No exception when service was never recorded.
    c.record_success("never-seen")
    c.reset("also-never-seen")

    # State stays clean: first failure still starts at 1.
    assert c.record_failure("never-seen") == CircuitAction(count=1, action="raise")


def test_circuit_action_is_frozen() -> None:
    a = CircuitAction(count=1, action="raise")
    with pytest.raises(FrozenInstanceError):
        a.count = 2  # type: ignore[misc]
