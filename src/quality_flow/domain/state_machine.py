"""Validation for run and attempt lifecycle transitions."""

from collections.abc import Mapping
from typing import TypeVar

from quality_flow.domain.enums import AttemptStatus, RunStatus


class InvalidStateTransition(ValueError):
    """Raised when a lifecycle state does not have the requested successor."""


_RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.COMPLETED, RunStatus.INFRA_FAILED, RunStatus.TIMED_OUT}
    ),
}

_ATTEMPT_TRANSITIONS: Mapping[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.DISPATCHED: frozenset({AttemptStatus.RUNNING}),
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.PASSED,
            AttemptStatus.TEST_FAILED,
            AttemptStatus.INFRA_FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.ABANDONED,
        }
    ),
}

Status = TypeVar("Status")


def ensure_run_transition(current: RunStatus, next_status: RunStatus) -> None:
    """Raise when a run is asked to move outside its declared lifecycle."""
    _ensure_transition(_RUN_TRANSITIONS, current, next_status)


def ensure_attempt_transition(
    current: AttemptStatus, next_status: AttemptStatus
) -> None:
    """Raise when an attempt is asked to move outside its declared lifecycle."""
    _ensure_transition(_ATTEMPT_TRANSITIONS, current, next_status)


def _ensure_transition(
    transitions: Mapping[Status, frozenset[Status]],
    current: Status,
    next_status: Status,
) -> None:
    if next_status not in transitions.get(current, frozenset()):
        raise InvalidStateTransition(f"Cannot transition from {current} to {next_status}")
