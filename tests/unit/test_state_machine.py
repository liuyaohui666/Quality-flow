from itertools import product

import pytest

from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
from quality_flow.domain.state_machine import (
    InvalidStateTransition,
    ensure_attempt_transition,
    ensure_run_transition,
    resolve_terminal_run_status,
)


def test_completed_run_cannot_return_to_running() -> None:
    with pytest.raises(InvalidStateTransition):
        ensure_run_transition(RunStatus.COMPLETED, RunStatus.RUNNING)


@pytest.mark.parametrize(
    ("current", "next_status"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.RUNNING, RunStatus.INFRA_FAILED),
        (RunStatus.RUNNING, RunStatus.TIMED_OUT),
    ],
)
def test_run_allows_only_declared_transitions(
    current: RunStatus, next_status: RunStatus
) -> None:
    ensure_run_transition(current, next_status)


@pytest.mark.parametrize(
    "next_status",
    [
        AttemptStatus.PASSED,
        AttemptStatus.TEST_FAILED,
        AttemptStatus.INFRA_FAILED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.ABANDONED,
    ],
)
def test_running_attempt_can_finish_with_each_terminal_status(
    next_status: AttemptStatus,
) -> None:
    ensure_attempt_transition(AttemptStatus.RUNNING, next_status)


def test_attempt_cannot_skip_dispatch_to_a_terminal_status() -> None:
    with pytest.raises(InvalidStateTransition):
        ensure_attempt_transition(AttemptStatus.DISPATCHED, AttemptStatus.PASSED)


@pytest.mark.parametrize(
    ("current", "next_status"),
    [
        (AttemptStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.RUNNING, AttemptStatus.INFRA_FAILED),
        ("running", RunStatus.COMPLETED),
        (RunStatus.RUNNING, "completed"),
    ],
)
def test_run_transition_rejects_other_enum_classes_and_strings(
    current: object, next_status: object
) -> None:
    with pytest.raises(InvalidStateTransition):
        ensure_run_transition(current, next_status)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current", "next_status"),
    [
        (RunStatus.RUNNING, AttemptStatus.PASSED),
        (AttemptStatus.RUNNING, RunStatus.INFRA_FAILED),
        ("running", AttemptStatus.PASSED),
        (AttemptStatus.RUNNING, "passed"),
    ],
)
def test_attempt_transition_rejects_other_enum_classes_and_strings(
    current: object, next_status: object
) -> None:
    with pytest.raises(InvalidStateTransition):
        ensure_attempt_transition(current, next_status)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("attempt_status", "outcome", "run_status"),
    [
        (AttemptStatus.PASSED, RunOutcome.PASSED, RunStatus.COMPLETED),
        (AttemptStatus.PASSED, RunOutcome.FAILED, RunStatus.COMPLETED),
        (AttemptStatus.TEST_FAILED, RunOutcome.FAILED, RunStatus.COMPLETED),
        (AttemptStatus.INFRA_FAILED, RunOutcome.UNKNOWN, RunStatus.INFRA_FAILED),
        (AttemptStatus.TIMED_OUT, RunOutcome.UNKNOWN, RunStatus.TIMED_OUT),
        (AttemptStatus.ABANDONED, RunOutcome.UNKNOWN, RunStatus.INFRA_FAILED),
    ],
)
def test_terminal_result_matrix_accepts_only_trusted_combinations(
    attempt_status: AttemptStatus,
    outcome: RunOutcome,
    run_status: RunStatus,
) -> None:
    assert resolve_terminal_run_status(attempt_status, outcome) is run_status


def test_terminal_result_matrix_rejects_every_other_combination() -> None:
    valid = {
        (AttemptStatus.PASSED, RunOutcome.PASSED),
        (AttemptStatus.PASSED, RunOutcome.FAILED),
        (AttemptStatus.TEST_FAILED, RunOutcome.FAILED),
        (AttemptStatus.INFRA_FAILED, RunOutcome.UNKNOWN),
        (AttemptStatus.TIMED_OUT, RunOutcome.UNKNOWN),
        (AttemptStatus.ABANDONED, RunOutcome.UNKNOWN),
    }

    for combination in product(AttemptStatus, RunOutcome):
        if combination in valid:
            continue
        with pytest.raises(InvalidStateTransition):
            resolve_terminal_run_status(*combination)
