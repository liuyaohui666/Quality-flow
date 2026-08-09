"""Validation for run and attempt lifecycle transitions."""

from collections.abc import Mapping
from typing import TypeVar

from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus


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

_TERMINAL_RESULT_MATRIX: Mapping[
    tuple[AttemptStatus, RunOutcome], RunStatus
] = {
    (AttemptStatus.PASSED, RunOutcome.PASSED): RunStatus.COMPLETED,
    (AttemptStatus.PASSED, RunOutcome.FAILED): RunStatus.COMPLETED,
    (AttemptStatus.TEST_FAILED, RunOutcome.FAILED): RunStatus.COMPLETED,
    (AttemptStatus.INFRA_FAILED, RunOutcome.UNKNOWN): RunStatus.INFRA_FAILED,
    (AttemptStatus.TIMED_OUT, RunOutcome.UNKNOWN): RunStatus.TIMED_OUT,
    (AttemptStatus.ABANDONED, RunOutcome.UNKNOWN): RunStatus.INFRA_FAILED,
}

Status = TypeVar("Status")


def ensure_run_transition(current: RunStatus, next_status: RunStatus) -> None:
    """Raise when a run is asked to move outside its declared lifecycle."""
    if type(current) is not RunStatus or type(next_status) is not RunStatus:
        raise InvalidStateTransition("Run transitions require RunStatus values")
    _ensure_transition(_RUN_TRANSITIONS, current, next_status)


def ensure_attempt_transition(
    current: AttemptStatus, next_status: AttemptStatus
) -> None:
    """Raise when an attempt is asked to move outside its declared lifecycle."""
    if type(current) is not AttemptStatus or type(next_status) is not AttemptStatus:
        raise InvalidStateTransition("Attempt transitions require AttemptStatus values")
    _ensure_transition(_ATTEMPT_TRANSITIONS, current, next_status)


def resolve_terminal_run_status(
    attempt_status: AttemptStatus, outcome: RunOutcome
) -> RunStatus:
    """Resolve the Run terminal status for a trusted Attempt/Outcome pair."""
    if type(attempt_status) is not AttemptStatus or type(outcome) is not RunOutcome:
        raise InvalidStateTransition(
            "Terminal results require AttemptStatus and RunOutcome values"
        )
    try:
        return _TERMINAL_RESULT_MATRIX[(attempt_status, outcome)]
    except KeyError as error:
        raise InvalidStateTransition(
            f"Invalid terminal result: {attempt_status} with {outcome}"
        ) from error


def _ensure_transition(
    transitions: Mapping[Status, frozenset[Status]],
    current: Status,
    next_status: Status,
) -> None:
    if next_status not in transitions.get(current, frozenset()):
        raise InvalidStateTransition(f"Cannot transition from {current} to {next_status}")
