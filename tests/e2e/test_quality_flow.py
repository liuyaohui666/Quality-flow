from __future__ import annotations

from collections.abc import Iterator
import os
import re
import time
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest


TERMINAL_STATUSES = {"completed", "infra_failed", "timed_out"}
SCENARIOS = (
    ("demo-api", "ok", "completed", "passed"),
    ("demo-api", "error", "completed", "failed"),
    ("demo-api", "slow", "timed_out", "unknown"),
    ("demo-load", "baseline", "completed", "passed"),
    ("demo-load", "degraded", "completed", "failed"),
)


@pytest.fixture(scope="session")
def api_client() -> Iterator[httpx.Client]:
    base_url = os.environ.get("QUALITY_FLOW_API_URL", "http://127.0.0.1:18000")
    with httpx.Client(base_url=base_url, timeout=10.0, trust_env=False) as client:
        yield client


def wait_for_terminal_run(
    api_client: httpx.Client,
    run_id: str,
    *,
    expected: tuple[str, str],
    timeout_seconds: float = 90,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_observed: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = api_client.get(f"/api/v1/runs/{run_id}")
        response.raise_for_status()
        last_observed = response.json()
        if last_observed["status"] in TERMINAL_STATUSES:
            actual = (last_observed["status"], last_observed["outcome"])
            assert actual == expected, (
                f"run {run_id} reached unexpected terminal pair {actual}; "
                f"expected {expected}; payload={last_observed}"
            )
            return last_observed
        time.sleep(0.25)

    events = api_client.get(f"/api/v1/runs/{run_id}/events")
    event_payload = events.json() if events.is_success else {"status": events.status_code}
    pytest.fail(
        f"run {run_id} did not become terminal within {timeout_seconds}s; "
        f"last_observed={last_observed}; events={event_payload}"
    )


def _submit(
    api_client: httpx.Client, suite_id: str, scenario: str, *, key: str
) -> str:
    response = api_client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": key},
        json={"suite_id": suite_id, "parameters": {"scenario": scenario}},
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def _assert_path_free(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = {"uri", "path", "storage_uri", "working_directory"}
        assert forbidden.isdisjoint(value)
        for nested in value.values():
            _assert_path_free(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_path_free(nested)


def _observe_scenario(
    api_client: httpx.Client,
    suite_id: str,
    scenario: str,
    *,
    expected: tuple[str, str],
) -> dict[str, Any]:
    run_id = _submit(
        api_client,
        suite_id,
        scenario,
        key=f"e2e-{suite_id}-{scenario}-{uuid4()}",
    )
    run = wait_for_terminal_run(
        api_client, run_id, expected=expected, timeout_seconds=90
    )
    events_response = api_client.get(f"/api/v1/runs/{run_id}/events")
    artifacts_response = api_client.get(f"/api/v1/runs/{run_id}/artifacts")
    events_response.raise_for_status()
    artifacts_response.raise_for_status()
    return {
        "run": run,
        "events": events_response.json()["events"],
        "artifacts": artifacts_response.json()["artifacts"],
    }


@pytest.mark.parametrize(
    ("suite_id", "scenario", "status", "outcome"), SCENARIOS
)
def test_registered_demo_scenarios(
    api_client: httpx.Client,
    suite_id: str,
    scenario: str,
    status: str,
    outcome: str,
) -> None:
    evidence = _observe_scenario(
        api_client, suite_id, scenario, expected=(status, outcome)
    )
    run = evidence["run"]
    events = evidence["events"]
    artifacts = evidence["artifacts"]

    assert (run["status"], run["outcome"]) == (status, outcome)
    assert len(run["attempts"]) == 1
    assert [event["event_type"] for event in events].count("run.queued") == 1
    assert [event["event_type"] for event in events].count("run.started") == 1
    assert [event["event_type"] for event in events].count("run.finished") == 1
    terminal_events = [event for event in events if event["event_type"] == "run.finished"]
    assert [(event["status"], event["outcome"]) for event in terminal_events] == [
        (status, outcome)
    ]

    assert artifacts
    for artifact in artifacts:
        UUID(artifact["artifact_id"])
        UUID(artifact["attempt_id"])
        assert re.fullmatch(r"[0-9a-f]{64}", artifact["checksum"])
        assert artifact["size_bytes"] >= 0
        assert artifact["mime_type"] in {"application/xml", "text/csv", "text/plain"}
    _assert_path_free(run)
    _assert_path_free({"events": events, "artifacts": artifacts})

    if suite_id == "demo-api" and scenario != "slow":
        expected_summary = (
            {"total": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0}
            if scenario == "ok"
            else {"total": 1, "passed": 0, "failed": 1, "errors": 0, "skipped": 0}
        )
        assert run["case_summary"] == expected_summary
        assert run["attempts"][0]["status"] == (
            "passed" if scenario == "ok" else "test_failed"
        )
        assert sorted(artifact["artifact_type"] for artifact in artifacts) == [
            "junit_xml",
            "stderr",
            "stdout",
        ]
        assert len(run["gates"]) == 1
        assert run["gates"][0]["gate_type"] == "functional"
        assert run["gates"][0]["passed"] is (scenario == "ok")
    elif scenario == "slow":
        assert run["attempts"][0]["status"] == "timed_out"
        assert run["case_summary"] == {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        }
        assert run["gates"] == []
        assert all(artifact["artifact_type"] != "junit_xml" for artifact in artifacts)
    else:
        metrics = {metric["name"]: metric["value"] for metric in run["metrics"]}
        assert metrics["request_count"] >= 3
        assert metrics["failure_ratio"] == 0
        assert len(run["gates"]) == 1
        assert run["gates"][0]["gate_type"] == "performance"
        assert run["gates"][0]["passed"] is (scenario == "baseline")
        if scenario == "degraded":
            assert metrics["p95_ms"] > 250
            assert "p95_ms" in run["gates"][0]["reason_codes"]


def test_duplicate_submission_has_one_effective_attempt_and_terminal_event(
    api_client: httpx.Client,
) -> None:
    key = f"e2e-duplicate-{uuid4()}"
    body = {"suite_id": "demo-api", "parameters": {"scenario": "ok"}}
    first = api_client.post(
        "/api/v1/runs", headers={"Idempotency-Key": key}, json=body
    )
    second = api_client.post(
        "/api/v1/runs", headers={"Idempotency-Key": key}, json=body
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    run_id = first.json()["run_id"]
    run = wait_for_terminal_run(
        api_client, run_id, expected=("completed", "passed"), timeout_seconds=90
    )
    events = api_client.get(f"/api/v1/runs/{run_id}/events").json()["events"]
    artifacts = api_client.get(f"/api/v1/runs/{run_id}/artifacts").json()[
        "artifacts"
    ]

    assert len(run["attempts"]) == 1
    assert run["case_summary"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert run["gates"] == [
        {"gate_type": "functional", "passed": True, "reason_codes": []}
    ]
    assert sorted(artifact["artifact_type"] for artifact in artifacts) == [
        "junit_xml",
        "stderr",
        "stdout",
    ]
    assert {artifact["attempt_id"] for artifact in artifacts} == {
        run["attempts"][0]["attempt_id"]
    }
    assert [event["event_type"] for event in events].count("run.queued") == 1
    assert [event["event_type"] for event in events].count("run.started") == 1
    assert [event["event_type"] for event in events].count("run.finished") == 1


def test_artifacts_are_owned_by_distinct_attempts(
    api_client: httpx.Client,
) -> None:
    first = _observe_scenario(
        api_client, "demo-api", "ok", expected=("completed", "passed")
    )
    second = _observe_scenario(
        api_client, "demo-api", "error", expected=("completed", "failed")
    )
    first_ids = {artifact["artifact_id"] for artifact in first["artifacts"]}
    second_ids = {artifact["artifact_id"] for artifact in second["artifacts"]}
    first_attempts = {artifact["attempt_id"] for artifact in first["artifacts"]}
    second_attempts = {artifact["attempt_id"] for artifact in second["artifacts"]}

    assert first_ids.isdisjoint(second_ids)
    assert first_attempts == {first["run"]["attempts"][0]["attempt_id"]}
    assert second_attempts == {second["run"]["attempts"][0]["attempt_id"]}
    assert first_attempts.isdisjoint(second_attempts)
