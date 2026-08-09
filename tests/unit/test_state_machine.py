import pytest

from quality_flow.domain.enums import AttemptStatus, RunStatus
from quality_flow.domain.state_machine import (
    InvalidStateTransition,
    ensure_attempt_transition,
    ensure_run_transition,
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
