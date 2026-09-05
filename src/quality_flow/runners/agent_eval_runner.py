"""Bounded, rule-based evaluation of structured HTTP Agent applications."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import time
from typing import Any

import httpx

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.agent_eval_definition import (
    AgentEvalCase,
    AgentEvalDefinition,
    AgentEvalDefinitionError,
)
from quality_flow.runners.base import (
    CaseResultData,
    CaseSummary,
    ExecutionSpec,
    MetricData,
    RunnerArtifact,
    RunnerOutcome,
)
from quality_flow.runners.gates import evaluate_functional_gate
from quality_flow.runners.subprocess_runner import (
    RunnerConfigurationError,
    prepare_staging_directory,
    validate_suite_path,
    validate_workspace,
)
from quality_flow.runners.workflow_definition import (
    WorkflowDefinitionError,
    render_template,
)


_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
)
_MAX_REPORT_BODY_BYTES = 32 * 1024


class AgentEvalRunner:
    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        staging_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._environment = dict(environment or {})
        self._transport = transport
        self._staging_root = Path(staging_root) if staging_root is not None else None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic

    def run(
        self,
        spec: ExecutionSpec,
        workspace: Path,
        heartbeat: Callable[[], None],
    ) -> RunnerOutcome:
        started_at = self._clock()
        deadline = self._monotonic() + spec.timeout_seconds
        try:
            resolved_workspace = validate_workspace(
                workspace, spec.allowed_workspace_root
            )
            definition = AgentEvalDefinition.from_yaml(
                _definition_path(spec.argv, resolved_workspace)
            )
            context: dict[str, Any] = {
                "request": dict(spec.request_body or {}),
                "parameters": dict(spec.parameters),
                "env": dict(self._environment),
            }
            base_url = _validated_base_url(render_template(definition.base_url, context))
        except (
            AgentEvalDefinitionError,
            RunnerConfigurationError,
            WorkflowDefinitionError,
            TypeError,
            ValueError,
        ) as error:
            return RunnerOutcome(
                attempt_status=AttemptStatus.INFRA_FAILED,
                exit_code=None,
                started_at=started_at,
                finished_at=self._clock(),
                failure_kind="agent_eval_configuration",
                failure_summary=f"Agent evaluation configuration is invalid: {error}",
            )

        cases: list[CaseResultData] = []
        reports: list[dict[str, Any]] = []
        latencies: list[float] = []
        token_total = 0
        tool_violation_count = 0
        timed_out = False

        with httpx.Client(
            base_url=base_url,
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            for index, eval_case in enumerate(definition.cases):
                if timed_out or self._monotonic() >= deadline:
                    timed_out = True
                    for remaining_case in definition.cases[index:]:
                        cases.append(_skipped_case(remaining_case, "Run deadline exceeded"))
                        reports.append(
                            _skipped_report(remaining_case, "Run deadline exceeded")
                        )
                    break
                outcome = self._run_case(
                    eval_case,
                    client=client,
                    definition=definition,
                    context=context,
                    deadline=deadline,
                    heartbeat=heartbeat,
                )
                cases.append(outcome.case)
                reports.append(outcome.report)
                if outcome.executed:
                    latencies.append(outcome.duration_ms)
                token_total += outcome.token_total
                tool_violation_count += int(outcome.tool_violation)
                timed_out = outcome.timed_out

        summary = _case_summary(cases)
        metrics = _metrics(
            summary,
            latencies=latencies,
            token_total=token_total,
            tool_violation_count=tool_violation_count,
        )
        gate = evaluate_functional_gate(summary, spec.gate_policy)
        finished_at = self._clock()
        try:
            artifact = self._write_report(
                resolved_workspace,
                definition,
                reports,
                metrics,
                started_at,
                finished_at,
            )
        except (OSError, RunnerConfigurationError, TypeError, ValueError) as error:
            return RunnerOutcome(
                attempt_status=AttemptStatus.INFRA_FAILED,
                exit_code=None,
                started_at=started_at,
                finished_at=finished_at,
                case_results=tuple(cases),
                case_summary=summary,
                metrics=metrics,
                gate_result=gate,
                failure_kind="agent_eval_report",
                failure_summary=f"Agent evaluation report could not be written: {error}",
            )

        if timed_out:
            attempt_status = AttemptStatus.TIMED_OUT
            exit_code = None
            failure_kind = "timeout"
            failure_summary = "Agent evaluation exceeded its configured Run timeout"
        elif any(case.status != "passed" for case in cases) or not gate.passed:
            attempt_status = AttemptStatus.TEST_FAILED
            exit_code = 1
            failure_kind = "agent_eval_failed"
            failure_summary = "one or more Agent evaluation cases failed"
        else:
            attempt_status = AttemptStatus.PASSED
            exit_code = 0
            failure_kind = None
            failure_summary = None
        return RunnerOutcome(
            attempt_status=attempt_status,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            case_results=tuple(cases),
            case_summary=summary,
            metrics=metrics,
            gate_result=gate,
            artifacts=(artifact,),
            failure_kind=failure_kind,
            failure_summary=failure_summary,
        )

    def _run_case(
        self,
        eval_case: AgentEvalCase,
        *,
        client: httpx.Client,
        definition: AgentEvalDefinition,
        context: Mapping[str, Any],
        deadline: float,
        heartbeat: Callable[[], None],
    ) -> "_CaseOutcome":
        case_started = self._monotonic()
        deadline_limited_request = False
        try:
            prompt = render_template(eval_case.prompt, context)
            if not isinstance(prompt, str) or not prompt.strip():
                raise AgentEvalDefinitionError("rendered prompt must be non-empty text")
            heartbeat()
            remaining = max(deadline - self._monotonic(), 0.001)
            deadline_limited_request = remaining <= definition.request_timeout_seconds
            response = client.post(
                definition.path,
                json={"prompt": prompt},
                timeout=min(definition.request_timeout_seconds, remaining),
            )
            duration_ms = (self._monotonic() - case_started) * 1000
            response_body = _response_body(response)
            reasons, tool_violation, token_total = _evaluate_response(
                eval_case, response.status_code, response_body
            )
            status = "passed" if not reasons else "failed"
            message = "; ".join(reasons) if reasons else None
            return _CaseOutcome(
                case=CaseResultData(
                    node_id=f"agent_eval::{eval_case.case_id}",
                    status=status,
                    duration_ms=duration_ms,
                    message=message,
                ),
                report={
                    "id": eval_case.case_id,
                    "name": eval_case.name,
                    "status": status,
                    "duration_ms": duration_ms,
                    "message": message,
                    "prompt": prompt,
                    "response_status": response.status_code,
                    "response": _bounded_body(_sanitize(response_body)),
                },
                duration_ms=duration_ms,
                token_total=token_total,
                tool_violation=tool_violation,
            )
        except httpx.TimeoutException as error:
            duration_ms = (self._monotonic() - case_started) * 1000
            if deadline_limited_request:
                message = "Run deadline exceeded during Agent request"
                return _error_outcome(
                    eval_case,
                    message,
                    duration_ms,
                    timed_out=True,
                )
            return _error_outcome(
                eval_case,
                f"Agent request timed out: {error}",
                duration_ms,
            )
        except httpx.HTTPError as error:
            return _error_outcome(
                eval_case,
                f"Agent request failed: {error}",
                (self._monotonic() - case_started) * 1000,
            )
        except (AgentEvalDefinitionError, WorkflowDefinitionError, TypeError, ValueError) as error:
            return _error_outcome(
                eval_case,
                f"Agent evaluation case could not be rendered: {error}",
                (self._monotonic() - case_started) * 1000,
            )

    def _write_report(
        self,
        workspace: Path,
        definition: AgentEvalDefinition,
        cases: list[dict[str, Any]],
        metrics: tuple[MetricData, ...],
        started_at: datetime,
        finished_at: datetime,
    ) -> RunnerArtifact:
        staging = prepare_staging_directory(
            workspace, staging_parent=self._staging_root
        )
        report_path = staging / "agent-eval-report.json"
        report_path.write_text(
            json.dumps(
                {
                    "evaluation": definition.name,
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "metrics": {
                        metric.name: {"value": metric.value, "unit": metric.unit}
                        for metric in metrics
                    },
                    "cases": cases,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return RunnerArtifact(
            artifact_type="agent_eval_report",
            source_path=report_path,
            source_root=staging,
            mime_type="application/json",
        )


class _CaseOutcome:
    def __init__(
        self,
        *,
        case: CaseResultData,
        report: dict[str, Any],
        duration_ms: float,
        token_total: int = 0,
        tool_violation: bool = False,
        timed_out: bool = False,
        executed: bool = True,
    ) -> None:
        self.case = case
        self.report = report
        self.duration_ms = duration_ms
        self.token_total = token_total
        self.tool_violation = tool_violation
        self.timed_out = timed_out
        self.executed = executed


def _definition_path(argv: tuple[str, ...], workspace: Path) -> Path:
    if len(argv) != 2 or argv[0] != "agent_eval":
        raise RunnerConfigurationError(
            "agent_eval argv must be exactly: agent_eval <relative-yaml-path>"
        )
    if Path(argv[1]).suffix.casefold() not in {".yaml", ".yml"}:
        raise RunnerConfigurationError("Agent evaluation definition must be YAML")
    return validate_suite_path(argv[1], workspace, regular_file=True)


def _validated_base_url(value: Any) -> str:
    if not isinstance(value, str):
        raise AgentEvalDefinitionError("Agent evaluation base_url must render to text")
    url = httpx.URL(value)
    if url.scheme not in {"http", "https"} or not url.host:
        raise AgentEvalDefinitionError("Agent evaluation base_url must be HTTP(S)")
    if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        raise AgentEvalDefinitionError(
            "Agent evaluation base_url must be an origin without credentials, path, query, or fragment"
        )
    return str(url)


def _evaluate_response(
    eval_case: AgentEvalCase, status_code: int, body: Any
) -> tuple[list[str], bool, int]:
    expected = eval_case.expectation
    reasons: list[str] = []
    if status_code != expected.status:
        reasons.append(f"expected status {expected.status}, got {status_code}")
    if not _valid_agent_response(body):
        reasons.append("structured response contract is invalid")
        return reasons, False, 0

    if body["decision"] != expected.decision:
        reasons.append(
            f"expected decision {expected.decision}, got {body['decision']}"
        )
    answer = body["answer"].casefold()
    missing_fragments = [
        fragment for fragment in expected.answer_contains if fragment.casefold() not in answer
    ]
    if missing_fragments:
        reasons.append("answer missing fragments: " + ", ".join(missing_fragments))
    actual_tools = {call["name"] for call in body["tool_calls"]}
    disallowed_tools = actual_tools - expected.allowed_tools
    missing_tools = expected.required_tools - actual_tools
    tool_violation = bool(disallowed_tools)
    if disallowed_tools:
        reasons.append("disallowed tools: " + ", ".join(sorted(disallowed_tools)))
    if missing_tools:
        reasons.append("missing required tools: " + ", ".join(sorted(missing_tools)))
    if expected.forbid_scope_expansion and body["scope_expanded"]:
        reasons.append("scope expansion is forbidden")
    output_tokens = body["usage"]["output_tokens"]
    if (
        expected.max_output_tokens is not None
        and output_tokens > expected.max_output_tokens
    ):
        reasons.append(
            f"output token limit exceeded: {output_tokens} > {expected.max_output_tokens}"
        )
    return (
        reasons,
        tool_violation,
        body["usage"]["input_tokens"] + output_tokens,
    )


def _valid_agent_response(body: Any) -> bool:
    if not isinstance(body, Mapping):
        return False
    if body.get("decision") not in {"answer", "tool_call", "refuse"}:
        return False
    if not isinstance(body.get("answer"), str):
        return False
    if type(body.get("scope_expanded")) is not bool:
        return False
    tool_calls = body.get("tool_calls")
    if not isinstance(tool_calls, list) or not all(
        isinstance(call, Mapping)
        and isinstance(call.get("name"), str)
        and call.get("name")
        and isinstance(call.get("arguments"), Mapping)
        for call in tool_calls
    ):
        return False
    usage = body.get("usage")
    return isinstance(usage, Mapping) and all(
        type(usage.get(name)) is int and usage[name] >= 0
        for name in ("input_tokens", "output_tokens")
    )


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeError):
        return response.text


def _metrics(
    summary: CaseSummary,
    *,
    latencies: list[float],
    token_total: int,
    tool_violation_count: int,
) -> tuple[MetricData, ...]:
    total = max(summary.total, 1)
    ordered = sorted(latencies)
    p95_ms = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else 0.0
    return (
        MetricData("agent_pass_rate", summary.passed / total, "ratio"),
        MetricData("tool_violation_rate", tool_violation_count / total, "ratio"),
        MetricData("agent_p95_latency_ms", p95_ms, "ms"),
        MetricData("total_tokens", token_total, "count"),
    )


def _case_summary(cases: list[CaseResultData]) -> CaseSummary:
    return CaseSummary(
        total=len(cases),
        passed=sum(case.status == "passed" for case in cases),
        failed=sum(case.status == "failed" for case in cases),
        errors=sum(case.status == "error" for case in cases),
        skipped=sum(case.status == "skipped" for case in cases),
    )


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if key is not None and any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): _sanitize(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _bounded_body(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= _MAX_REPORT_BODY_BYTES:
        return value
    return f"[TRUNCATED body exceeded {_MAX_REPORT_BODY_BYTES} bytes]"


def _error_outcome(
    eval_case: AgentEvalCase,
    message: str,
    duration_ms: float,
    *,
    timed_out: bool = False,
) -> _CaseOutcome:
    return _CaseOutcome(
        case=CaseResultData(
            node_id=f"agent_eval::{eval_case.case_id}",
            status="error",
            duration_ms=duration_ms,
            message=message,
        ),
        report={
            "id": eval_case.case_id,
            "name": eval_case.name,
            "status": "error",
            "duration_ms": duration_ms,
            "message": message,
        },
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


def _skipped_case(eval_case: AgentEvalCase, reason: str) -> CaseResultData:
    return CaseResultData(
        node_id=f"agent_eval::{eval_case.case_id}",
        status="skipped",
        duration_ms=0.0,
        message=reason,
    )


def _skipped_report(eval_case: AgentEvalCase, reason: str) -> dict[str, Any]:
    return {
        "id": eval_case.case_id,
        "name": eval_case.name,
        "status": "skipped",
        "duration_ms": 0.0,
        "message": reason,
    }
