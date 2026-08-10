from __future__ import annotations

import json

import pytest

from scripts import wait_for_run


class FakeResponse:
    def __init__(self, payload: dict[str, str]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return self._payload


class FakeClient:
    def __init__(self, payloads: list[dict[str, str]]) -> None:
        self._payloads = iter(payloads)

    def get(self, _path: str) -> FakeResponse:
        return FakeResponse(next(self._payloads))


def test_wait_for_terminal_returns_the_first_terminal_payload() -> None:
    client = FakeClient(
        [
            {"status": "queued", "outcome": "unknown"},
            {"status": "completed", "outcome": "passed"},
        ]
    )
    ticks = iter((0.0, 0.0, 0.1, 0.2))

    result = wait_for_run.wait_for_terminal(
        client,
        "4e908c97-689f-4e1e-9c37-5eb508c2aca3",
        timeout_seconds=1,
        poll_interval=0.01,
        monotonic=lambda: next(ticks),
        sleep=lambda _seconds: None,
    )

    assert result == {"status": "completed", "outcome": "passed"}
    assert wait_for_run.terminal_exit_code(result) == 0


def test_wait_timeout_carries_last_observed_json() -> None:
    client = FakeClient([{"status": "running", "outcome": "unknown"}])
    ticks = iter((0.0, 0.1, 1.0))

    with pytest.raises(wait_for_run.WaitTimeout) as captured:
        wait_for_run.wait_for_terminal(
            client,
            "4e908c97-689f-4e1e-9c37-5eb508c2aca3",
            timeout_seconds=1,
            poll_interval=0.01,
            monotonic=lambda: next(ticks),
            sleep=lambda _seconds: None,
        )

    assert captured.value.last_observed == {
        "status": "running",
        "outcome": "unknown",
    }
    assert json.loads(str(captured.value))["last_observed"]["status"] == "running"


@pytest.mark.parametrize(
    ("payload", "exit_code"),
    [
        ({"status": "completed", "outcome": "failed"}, 1),
        ({"status": "timed_out", "outcome": "unknown"}, 1),
        ({"status": "infra_failed", "outcome": "unknown"}, 1),
    ],
)
def test_terminal_nonpassing_runs_use_failure_exit_code(
    payload: dict[str, str], exit_code: int
) -> None:
    assert wait_for_run.terminal_exit_code(payload) == exit_code
