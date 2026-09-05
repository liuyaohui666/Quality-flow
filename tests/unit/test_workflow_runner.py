from __future__ import annotations

import json
from pathlib import Path

import httpx

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.base import ExecutionSpec
from quality_flow.runners.workflow_runner import WorkflowRunner


def _spec(tmp_path: Path, *, timeout: float = 5) -> ExecutionSpec:
    return ExecutionSpec(
        argv=("workflow", "workflow.yaml"),
        timeout_seconds=timeout,
        allowed_workspace_root=tmp_path,
        parameters={"scenario": "smoke"},
        request_body={"name": "Ada", "token": "top-secret"},
    )


def _write(tmp_path: Path, workflow: str) -> None:
    (tmp_path / "workflow.yaml").write_text(workflow, encoding="utf-8")


def _happy_definition() -> str:
    return """
version: 1
name: resource-lifecycle
base_url: "{{ env.QUALITY_FLOW_TARGET_URL }}"
request_timeout_seconds: 2
steps:
  - id: create
    name: Create resource
    request:
      method: POST
      path: /resources
      headers:
        Authorization: "Bearer {{ request.token }}"
      json:
        name: "{{ request.name }}"
    expect:
      status: 201
      json_contains:
        name: "{{ request.name }}"
    capture:
      resource_id: $.id
  - id: read
    name: Read resource
    request:
      method: GET
      path: "/resources/{{ resource_id }}"
      query:
        scenario: "{{ parameters.scenario }}"
    expect:
      status: 200
      json_schema:
        type: object
        required: [id, name]
cleanup:
  - id: delete
    name: Delete resource
    when_variables: [resource_id]
    request:
      method: DELETE
      path: "/resources/{{ resource_id }}"
    expect:
      status: 204
"""


def test_runner_passes_captured_value_to_next_step_and_cleanup(
    tmp_path: Path,
) -> None:
    _write(tmp_path, _happy_definition())
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"id": "res-7", "name": "Ada", "access_token": "leak-me-not"},
            )
        if request.method == "GET":
            return httpx.Response(200, json={"id": "res-7", "name": "Ada"})
        return httpx.Response(204)

    runner = WorkflowRunner(
        environment={"QUALITY_FLOW_TARGET_URL": "http://target.test"},
        transport=httpx.MockTransport(handler),
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
    )

    result = runner.run(_spec(tmp_path), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.PASSED
    assert [(case.node_id, case.status) for case in result.case_results] == [
        ("workflow::create", "passed"),
        ("workflow::read", "passed"),
        ("workflow::delete", "passed"),
    ]
    assert requests == [
        ("POST", "http://target.test/resources"),
        ("GET", "http://target.test/resources/res-7?scenario=smoke"),
        ("DELETE", "http://target.test/resources/res-7"),
    ]
    assert result.gate_result is not None and result.gate_result.passed is True
    report = json.loads(result.artifacts[0].source_path.read_text(encoding="utf-8"))
    assert report["captured_variables"] == ["resource_id"]
    assert "top-secret" not in json.dumps(report)
    assert "leak-me-not" not in json.dumps(report)
    assert report["steps"][0]["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert report["steps"][0]["response"]["body"]["access_token"] == "[REDACTED]"


def test_runner_stops_main_steps_after_assertion_failure_and_skips_cleanup(
    tmp_path: Path,
) -> None:
    _write(tmp_path, _happy_definition())

    runner = WorkflowRunner(
        environment={"QUALITY_FLOW_TARGET_URL": "http://target.test"},
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, json={})),
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
    )

    result = runner.run(_spec(tmp_path), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.TEST_FAILED
    assert [case.status for case in result.case_results] == [
        "failed",
        "skipped",
        "skipped",
    ]
    assert "expected status 201, got 500" in (result.case_results[0].message or "")


def test_runner_runs_cleanup_after_later_main_step_fails(tmp_path: Path) -> None:
    _write(tmp_path, _happy_definition())
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(201, json={"id": "res-8", "name": "Ada"})
        if request.method == "GET":
            return httpx.Response(503, json={"status": "down"})
        return httpx.Response(204)

    result = WorkflowRunner(
        environment={"QUALITY_FLOW_TARGET_URL": "http://target.test"},
        transport=httpx.MockTransport(handler),
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
    ).run(_spec(tmp_path), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.TEST_FAILED
    assert [case.status for case in result.case_results] == [
        "passed",
        "failed",
        "passed",
    ]
    assert methods == ["POST", "GET", "DELETE"]


def test_runner_treats_target_transport_error_as_test_error(tmp_path: Path) -> None:
    _write(tmp_path, _happy_definition())

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("target unavailable", request=request)

    result = WorkflowRunner(
        environment={"QUALITY_FLOW_TARGET_URL": "http://target.test"},
        transport=httpx.MockTransport(unavailable),
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
    ).run(_spec(tmp_path), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.TEST_FAILED
    assert result.case_results[0].status == "error"
    assert result.failure_kind == "workflow_request_error"


def test_runner_marks_invalid_definition_as_infrastructure_failure(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "version: 2\n")
    result = WorkflowRunner(
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging"
    ).run(
        _spec(tmp_path), tmp_path, lambda: None
    )

    assert result.attempt_status is AttemptStatus.INFRA_FAILED
    assert result.failure_kind == "workflow_configuration"
    assert result.case_results == ()


def test_runner_enforces_the_run_deadline_before_starting_a_step(
    tmp_path: Path,
) -> None:
    _write(tmp_path, _happy_definition())
    times = iter((0.0, 2.0, 2.0))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    result = WorkflowRunner(
        environment={"QUALITY_FLOW_TARGET_URL": "http://target.test"},
        transport=httpx.MockTransport(handler),
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
        monotonic=lambda: next(times),
    ).run(_spec(tmp_path, timeout=1), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.TIMED_OUT
    assert result.failure_kind == "timeout"
    assert requests == []
