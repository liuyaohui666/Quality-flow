from __future__ import annotations

import json
from pathlib import Path

import httpx

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.agent_eval_runner import AgentEvalRunner
from quality_flow.runners.base import ExecutionSpec


def _definition() -> str:
    return """
version: 1
name: demo-agent-eval
base_url: "{{ env.QUALITY_FLOW_TARGET_URL }}"
path: /agent/respond
request_timeout_seconds: 2
cases:
  - id: answer
    name: Answer normally
    prompt: "Explain {{ request.topic }} briefly"
    expect:
      status: 200
      decision: answer
      answer_contains: [quality]
      allowed_tools: []
      forbid_scope_expansion: true
      max_output_tokens: 30
  - id: weather
    name: Use weather tool
    prompt: Weather in Hefei
    expect:
      status: 200
      decision: tool_call
      answer_contains: [weather]
      allowed_tools: [weather.lookup]
      required_tools: [weather.lookup]
      forbid_scope_expansion: true
      max_output_tokens: 30
"""


def _spec(tmp_path: Path, *, timeout: float = 10) -> ExecutionSpec:
    return ExecutionSpec(
        argv=("agent_eval", "agent-eval.yaml"),
        timeout_seconds=timeout,
        allowed_workspace_root=tmp_path,
        request_body={"topic": "quality engineering", "api_token": "secret"},
    )


def _write(tmp_path: Path, definition: str | None = None) -> None:
    (tmp_path / "agent-eval.yaml").write_text(
        definition or _definition(), encoding="utf-8"
    )


def _response(
    *,
    decision: str,
    answer: str,
    tools: list[dict[str, object]] | None = None,
    scope_expanded: bool = False,
    output_tokens: int = 10,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "decision": decision,
            "answer": answer,
            "tool_calls": tools or [],
            "scope_expanded": scope_expanded,
            "usage": {"input_tokens": 5, "output_tokens": output_tokens},
            "access_token": "must-not-leak",
        },
    )


def test_runner_evaluates_cases_and_emits_metrics_and_sanitized_report(
    tmp_path: Path,
) -> None:
    _write(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["prompt"]
        if "Weather" in prompt:
            return _response(
                decision="tool_call",
                answer="I will use the weather service",
                tools=[{"name": "weather.lookup", "arguments": {"city": "Hefei"}}],
            )
        return _response(decision="answer", answer="Quality needs evidence")

    result = AgentEvalRunner(
        environment={"QUALITY_FLOW_TARGET_URL": "http://target.test"},
        transport=httpx.MockTransport(handler),
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
    ).run(_spec(tmp_path), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.PASSED
    assert [(case.node_id, case.status) for case in result.case_results] == [
        ("agent_eval::answer", "passed"),
        ("agent_eval::weather", "passed"),
    ]
    metrics = {metric.name: metric.value for metric in result.metrics}
    assert metrics["agent_pass_rate"] == 1
    assert metrics["tool_violation_rate"] == 0
    assert metrics["total_tokens"] == 30
    assert metrics["agent_p95_latency_ms"] >= 0
    report = json.loads(result.artifacts[0].source_path.read_text(encoding="utf-8"))
    assert report["evaluation"] == "demo-agent-eval"
    assert report["cases"][0]["prompt"] == "Explain quality engineering briefly"
    assert report["cases"][0]["response"]["access_token"] == "[REDACTED]"
    assert "must-not-leak" not in json.dumps(report)


def test_runner_reports_tool_scope_and_token_policy_violations(tmp_path: Path) -> None:
    _write(tmp_path, _definition().replace("cases:\n", "cases:\n").split("  - id: weather")[0])

    result = AgentEvalRunner(
        environment={"QUALITY_FLOW_TARGET_URL": "http://target.test"},
        transport=httpx.MockTransport(
            lambda _request: _response(
                decision="answer",
                answer="Quality response",
                tools=[{"name": "admin.delete", "arguments": {}}],
                scope_expanded=True,
                output_tokens=99,
            )
        ),
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
    ).run(_spec(tmp_path), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.TEST_FAILED
    message = result.case_results[0].message or ""
    assert "disallowed tools" in message
    assert "scope expansion" in message
    assert "output token limit" in message
    metrics = {metric.name: metric.value for metric in result.metrics}
    assert metrics["tool_violation_rate"] == 1


def test_runner_treats_invalid_structured_response_as_test_failure(
    tmp_path: Path,
) -> None:
    _write(tmp_path)
    result = AgentEvalRunner(
        environment={"QUALITY_FLOW_TARGET_URL": "http://target.test"},
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"answer": "missing fields"})
        ),
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
    ).run(_spec(tmp_path), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.TEST_FAILED
    assert result.case_results[0].status == "failed"
    assert "structured response" in (result.case_results[0].message or "")


def test_runner_marks_run_limited_request_timeout_as_timed_out(tmp_path: Path) -> None:
    _write(tmp_path)

    def times_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("budget exhausted", request=request)

    result = AgentEvalRunner(
        environment={"QUALITY_FLOW_TARGET_URL": "http://target.test"},
        transport=httpx.MockTransport(times_out),
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
    ).run(_spec(tmp_path, timeout=1), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.TIMED_OUT
    assert result.failure_kind == "timeout"


def test_runner_marks_invalid_definition_as_infrastructure_failure(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "version: 2\n")

    result = AgentEvalRunner(
        staging_root=tmp_path.parent / f"{tmp_path.name}-staging"
    ).run(_spec(tmp_path), tmp_path, lambda: None)

    assert result.attempt_status is AttemptStatus.INFRA_FAILED
    assert result.failure_kind == "agent_eval_configuration"
