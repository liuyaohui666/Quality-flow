"""Execution lifecycle values used by runs and their attempts."""

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    INFRA_FAILED = "infra_failed"
    TIMED_OUT = "timed_out"


class RunOutcome(StrEnum):
    UNKNOWN = "unknown"
    PASSED = "passed"
    FAILED = "failed"


class AttemptStatus(StrEnum):
    DISPATCHED = "dispatched"
    RUNNING = "running"
    PASSED = "passed"
    TEST_FAILED = "test_failed"
    INFRA_FAILED = "infra_failed"
    TIMED_OUT = "timed_out"
    ABANDONED = "abandoned"
