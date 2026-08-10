from __future__ import annotations

import httpx

from scripts.ci_gate import run_ci_gate


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def response(status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("GET", "https://api.example.test"))


def test_ci_gate_submits_and_returns_zero_only_for_completed_passed() -> None:
    calls: list[tuple[str, str]] = []
    replies = iter(
        [
            response(202, {"run_id": "run 1"}),
            response(200, {"status": "queued", "outcome": "unknown"}),
            response(200, {"status": "completed", "outcome": "passed"}),
        ]
    )
    clock = FakeClock()

    def request(method: str, url: str, **_: object) -> httpx.Response:
        calls.append((method, url))
        return next(replies)

    assert run_ci_gate(
        "https://api.example.test/base/",
        "demo-api",
        "smoke",
        poll_interval_seconds=1,
        timeout_seconds=5,
        request=request,
        clock=clock,
        sleep=clock.sleep,
    ) == 0
    assert calls == [
        ("POST", "https://api.example.test/base/api/v1/runs"),
        ("GET", "https://api.example.test/base/api/v1/runs/run%201"),
        ("GET", "https://api.example.test/base/api/v1/runs/run%201"),
    ]


def test_ci_gate_returns_nonzero_for_terminal_failure_http_json_and_timeout() -> None:
    clock = FakeClock()

    assert run_ci_gate(
        "https://api.example.test", "demo-api", "smoke", 1, 2,
        request=lambda *_args, **_kwargs: response(500, {}), clock=clock, sleep=clock.sleep,
    ) != 0
    assert run_ci_gate(
        "https://api.example.test", "demo-api", "smoke", 1, 2,
        request=lambda method, *_args, **_kwargs: response(202, {"run_id": "id"}) if method == "POST" else response(200, []),
        clock=clock, sleep=clock.sleep,
    ) != 0
    assert run_ci_gate(
        "https://api.example.test", "demo-api", "smoke", 1, 2,
        request=lambda method, *_args, **_kwargs: response(202, {"run_id": "id"}) if method == "POST" else response(200, {"status": "running", "outcome": "unknown"}),
        clock=clock, sleep=clock.sleep,
    ) != 0
    assert run_ci_gate(
        "https://api.example.test", "demo-api", "smoke", 1, 2,
        request=lambda method, *_args, **_kwargs: response(202, {"run_id": "id"}) if method == "POST" else response(200, {"status": "infra_failed", "outcome": "unknown"}),
        clock=clock, sleep=clock.sleep,
    ) != 0


def test_ci_gate_total_timeout_includes_submission_time() -> None:
    clock = FakeClock()
    calls: list[str] = []

    def request(method: str, *_args: object, **_kwargs: object) -> httpx.Response:
        calls.append(method)
        clock.now += 3
        return response(202, {"run_id": "id"})

    assert run_ci_gate(
        "https://api.example.test",
        "demo-api",
        "smoke",
        poll_interval_seconds=1,
        timeout_seconds=2,
        request=request,
        clock=clock,
        sleep=clock.sleep,
    ) != 0
    assert calls == ["POST"]


def test_ci_gate_rejects_passed_result_arriving_exactly_at_deadline() -> None:
    clock = FakeClock()

    def request(method: str, *_args: object, **_kwargs: object) -> httpx.Response:
        if method == "POST":
            return response(202, {"run_id": "id"})
        clock.now = 2
        return response(200, {"status": "completed", "outcome": "passed"})

    assert run_ci_gate(
        "https://api.example.test",
        "demo-api",
        "smoke",
        poll_interval_seconds=1,
        timeout_seconds=2,
        request=request,
        clock=clock,
        sleep=clock.sleep,
    ) != 0


def test_ci_gate_rejects_unsafe_or_nonabsolute_api_base_urls() -> None:
    calls: list[str] = []

    def request(method: str, *_args: object, **_kwargs: object) -> httpx.Response:
        calls.append(method)
        return response(500, {})

    for api_url in (
        "https://api.example.test/base?redirect=/elsewhere",
        "https://api.example.test/base#fragment",
        "/relative/base",
    ):
        assert run_ci_gate(
            api_url,
            "demo-api",
            "smoke",
            poll_interval_seconds=1,
            timeout_seconds=2,
            request=request,
            clock=FakeClock(),
            sleep=lambda _seconds: None,
        ) != 0

    assert calls == []
