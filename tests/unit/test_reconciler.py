import pytest

from quality_flow.application.reconciler import _allows_worker_lost_retry
from quality_flow.infrastructure.models import Run, RunAttempt


class RetryReasons(list[str]):
    pass


VALID_RETRY_POLICY = {
    "retry_policy": {"max_attempts": 2, "retry_on": ["worker_lost"]}
}


@pytest.mark.parametrize(
    ("suite_snapshot", "attempt_no"),
    [
        pytest.param({}, 1, id="missing-policy"),
        pytest.param(None, 1, id="null-root"),
        pytest.param([], 1, id="list-root"),
        pytest.param({"retry_policy": None}, 1, id="null-policy"),
        pytest.param({"retry_policy": []}, 1, id="list-policy"),
        pytest.param(
            {"retry_policy": {"max_attempts": 2}},
            1,
            id="missing-retry-on",
        ),
        pytest.param(
            {"retry_policy": {"retry_on": ["worker_lost"]}},
            1,
            id="missing-max-attempts",
        ),
        pytest.param(
            {
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["worker_lost"],
                    "unknown": True,
                }
            },
            1,
            id="unknown-policy-key",
        ),
        pytest.param(
            {"retry_policy": {"max_attempts": 2, "retry_on": ["timeout"]}},
            1,
            id="unknown-reason",
        ),
        pytest.param(
            {
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["worker_lost", "timeout"],
                }
            },
            1,
            id="extra-timeout-reason",
        ),
        pytest.param(
            {"retry_policy": {"max_attempts": True, "retry_on": ["worker_lost"]}},
            1,
            id="boolean-attempt-budget",
        ),
        pytest.param(
            {"retry_policy": {"max_attempts": 1, "retry_on": ["worker_lost"]}},
            1,
            id="attempt-budget-one",
        ),
        pytest.param(
            {"retry_policy": {"max_attempts": 3, "retry_on": ["worker_lost"]}},
            1,
            id="attempt-budget-three",
        ),
        pytest.param(VALID_RETRY_POLICY, 0, id="attempt-zero"),
        pytest.param(VALID_RETRY_POLICY, 2, id="attempt-two"),
        pytest.param(
            {
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": RetryReasons(["worker_lost"]),
                }
            },
            1,
            id="retry-reasons-list-subclass",
        ),
        pytest.param(
            {
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["worker_lost", "worker_lost"],
                }
            },
            1,
            id="duplicate-reason-not-normalized",
        ),
    ],
)
def test_worker_lost_retry_policy_fails_closed(
    suite_snapshot: object, attempt_no: int
) -> None:
    run = Run(suite_snapshot=suite_snapshot)
    attempt = RunAttempt(attempt_no=attempt_no)

    try:
        allowed = _allows_worker_lost_retry(run, attempt)
    except Exception as error:  # pragma: no cover - failure path under test
        pytest.fail(f"retry decision raised instead of failing closed: {error!r}")

    assert allowed is False
