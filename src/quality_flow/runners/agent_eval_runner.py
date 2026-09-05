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
from jsonschema import Draft202012Validator

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.agent_eval_definition import (
    AgentEvalCase,
    AgentEvalDefinition,
    AgentEvalDefinitionError,
    AgentExpectation,
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
        turn_count = 0
        sample_count = 0
        sample_passed = 0
        consistency_numerator = 0
        consistency_denominator = 0
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
                latencies.extend(outcome.latencies)
                token_total += outcome.token_total
                tool_violation_count += int(outcome.tool_violation)
                turn_count += outcome.turn_count
                sample_count += outcome.sample_count
                sample_passed += outcome.sample_passed
                consistency_numerator += outcome.consistency_numerator
                consistency_denominator += outcome.consistency_denominator
                timed_out = outcome.timed_out

        summary = _case_summary(cases)
        metrics = _metrics(
            summary,
            latencies=latencies,
            token_total=token_total,
            tool_violation_count=tool_violation_count,
            turn_count=turn_count,
            sample_count=sample_count,
            sample_passed=sample_passed,
            consistency_numerator=consistency_numerator,
            consistency_denominator=consistency_denominator,
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
        samples: list[_SampleOutcome] = []
        timed_out = False

        for sample_index in range(1, eval_case.sample_count + 1):
            if self._monotonic() >= deadline:
                timed_out = True
                break
            sample = self._run_sample(
                eval_case,
                client=client,
                definition=definition,
                context=context,
                deadline=deadline,
                heartbeat=heartbeat,
            )
            sample.report["index"] = sample_index
            samples.append(sample)
            if sample.timed_out:
                timed_out = True
                break

        executed = len(samples)
        passed = sum(sample.status == "passed" for sample in samples)
        pass_rate = passed / executed if executed else 0.0
        completed_signatures = [
            sample.behavior_signature
            for sample in samples
            if sample.status != "error"
        ]
        modal_count = max(
            (
                completed_signatures.count(signature)
                for signature in set(completed_signatures)
            ),
            default=0,
        )
        consistency_rate = (
            modal_count / len(completed_signatures) if completed_signatures else 0.0
        )
        reasons: list[str] = []
        if eval_case.sample_count == 1 and samples and samples[0].report.get("message"):
            reasons.append(str(samples[0].report["message"]))
        if pass_rate < eval_case.min_sample_pass_rate:
            reasons.append(
                "sample pass rate below threshold: "
                f"{pass_rate:.3f} < {eval_case.min_sample_pass_rate:.3f}"
            )
        if consistency_rate < eval_case.min_behavior_consistency_rate:
            reasons.append(
                "behavior consistency below threshold: "
                f"{consistency_rate:.3f} < "
                f"{eval_case.min_behavior_consistency_rate:.3f}"
            )
        if timed_out:
            reasons.append("Run deadline exceeded")

        status = "error" if timed_out else ("failed" if reasons else "passed")
        message = "; ".join(reasons) if reasons else None
        duration_ms = (self._monotonic() - case_started) * 1000
        report: dict[str, Any] = {
            "id": eval_case.case_id,
            "name": eval_case.name,
            "status": status,
            "duration_ms": duration_ms,
            "message": message,
            "sample_count": eval_case.sample_count,
            "executed_sample_count": executed,
            "min_sample_pass_rate": eval_case.min_sample_pass_rate,
            "sample_pass_rate": pass_rate,
            "min_behavior_consistency_rate": (
                eval_case.min_behavior_consistency_rate
            ),
            "behavior_consistency_rate": consistency_rate,
            "samples": [sample.report for sample in samples],
        }
        if eval_case.sample_count == 1 and samples:
            sample_report = samples[0].report
            for key in (
                "actual_tool_sequence",
                "decision_sequence",
                "total_tokens",
                "turns",
                "prompt",
                "response_status",
                "response",
            ):
                if key in sample_report:
                    report[key] = sample_report[key]

        return _CaseOutcome(
            case=CaseResultData(
                node_id=f"agent_eval::{eval_case.case_id}",
                status=status,
                duration_ms=duration_ms,
                message=message,
            ),
            report=report,
            latencies=tuple(
                latency for sample in samples for latency in sample.latencies
            ),
            token_total=sum(sample.token_total for sample in samples),
            tool_violation=any(sample.tool_violation for sample in samples),
            timed_out=timed_out,
            turn_count=sum(sample.turn_count for sample in samples),
            sample_count=executed,
            sample_passed=passed,
            consistency_numerator=modal_count,
            consistency_denominator=len(completed_signatures),
        )

    def _run_sample(
        self,
        eval_case: AgentEvalCase,
        *,
        client: httpx.Client,
        definition: AgentEvalDefinition,
        context: Mapping[str, Any],
        deadline: float,
        heartbeat: Callable[[], None],
    ) -> "_SampleOutcome":
        case_started = self._monotonic()
        messages: list[dict[str, str]] = []
        turn_reports: list[dict[str, Any]] = []
        latencies: list[float] = []
        actual_tool_sequence: list[str] = []
        decision_sequence: list[str] = []
        case_reasons: list[str] = []
        token_total = 0
        turn_count = 0
        tool_violation = False
        case_error = False
        timed_out = False

        for turn_index, turn in enumerate(eval_case.turns, start=1):
            turn_started = self._monotonic()
            prompt: str | None = None
            try:
                rendered = render_template(turn.prompt, context)
                if not isinstance(rendered, str) or not rendered.strip():
                    raise AgentEvalDefinitionError(
                        "rendered prompt must be non-empty text"
                    )
                prompt = rendered
                if self._monotonic() >= deadline:
                    timed_out = True
                    case_error = True
                    message = "Run deadline exceeded before Agent request"
                    turn_reports.append(
                        _turn_error_report(turn_index, prompt, message, 0.0)
                    )
                    case_reasons.append(f"turn {turn_index}: {message}")
                    break
                heartbeat()
                remaining = max(deadline - self._monotonic(), 0.001)
                deadline_limited_request = (
                    remaining <= definition.request_timeout_seconds
                )
                if eval_case.legacy_prompt:
                    payload: dict[str, Any] = {"prompt": prompt}
                else:
                    messages.append({"role": "user", "content": prompt})
                    payload = {"messages": [dict(message) for message in messages]}
                turn_count += 1
                response = client.post(
                    definition.path,
                    json=payload,
                    timeout=min(definition.request_timeout_seconds, remaining),
                )
                duration_ms = (self._monotonic() - turn_started) * 1000
                latencies.append(duration_ms)
                response_body = _response_body(response)
                reasons, turn_tool_violation, turn_tokens = _evaluate_response(
                    turn.expectation, response.status_code, response_body
                )
                token_total += turn_tokens
                tool_violation = tool_violation or turn_tool_violation
                valid_response = _valid_agent_response(response_body)
                if valid_response:
                    decision_sequence.append(response_body["decision"])
                    actual_tool_sequence.extend(
                        call["name"] for call in response_body["tool_calls"]
                    )
                    if not eval_case.legacy_prompt:
                        messages.append(
                            {"role": "assistant", "content": response_body["answer"]}
                        )
                turn_status = "passed" if not reasons else "failed"
                turn_message = "; ".join(reasons) if reasons else None
                turn_reports.append(
                    {
                        "index": turn_index,
                        "status": turn_status,
                        "duration_ms": duration_ms,
                        "message": turn_message,
                        "prompt": prompt,
                        "response_status": response.status_code,
                        "response": _bounded_body(_sanitize(response_body)),
                    }
                )
                case_reasons.extend(
                    f"turn {turn_index}: {reason}" for reason in reasons
                )
                if not valid_response:
                    break
            except httpx.TimeoutException as error:
                duration_ms = (self._monotonic() - turn_started) * 1000
                latencies.append(duration_ms)
                timed_out = deadline_limited_request
                case_error = True
                message = (
                    "Run deadline exceeded during Agent request"
                    if timed_out
                    else f"Agent request timed out: {error}"
                )
                turn_reports.append(
                    _turn_error_report(turn_index, prompt, message, duration_ms)
                )
                case_reasons.append(f"turn {turn_index}: {message}")
                break
            except httpx.HTTPError as error:
                duration_ms = (self._monotonic() - turn_started) * 1000
                latencies.append(duration_ms)
                case_error = True
                message = f"Agent request failed: {error}"
                turn_reports.append(
                    _turn_error_report(turn_index, prompt, message, duration_ms)
                )
                case_reasons.append(f"turn {turn_index}: {message}")
                break
            except (
                AgentEvalDefinitionError,
                WorkflowDefinitionError,
                TypeError,
                ValueError,
            ) as error:
                duration_ms = (self._monotonic() - turn_started) * 1000
                case_error = True
                message = f"Agent evaluation turn could not be rendered: {error}"
                turn_reports.append(
                    _turn_error_report(turn_index, prompt, message, duration_ms)
                )
                case_reasons.append(f"turn {turn_index}: {message}")
                break

        if not case_error:
            actual_sequence = tuple(actual_tool_sequence)
            if (
                eval_case.expected_tool_sequence
                and actual_sequence != eval_case.expected_tool_sequence
            ):
                case_reasons.append(
                    "tool sequence mismatch: expected "
                    f"{list(eval_case.expected_tool_sequence)}, got {actual_tool_sequence}"
                )
                tool_violation = True
            if (
                eval_case.max_total_tokens is not None
                and token_total > eval_case.max_total_tokens
            ):
                case_reasons.append(
                    "total token limit exceeded: "
                    f"{token_total} > {eval_case.max_total_tokens}"
                )

        duration_ms = (self._monotonic() - case_started) * 1000
        status = "error" if case_error else ("failed" if case_reasons else "passed")
        message = "; ".join(case_reasons) if case_reasons else None
        report: dict[str, Any] = {
            "id": eval_case.case_id,
            "name": eval_case.name,
            "status": status,
            "duration_ms": duration_ms,
            "message": message,
            "actual_tool_sequence": actual_tool_sequence,
            "decision_sequence": decision_sequence,
            "total_tokens": token_total,
            "turns": turn_reports,
        }
        if eval_case.legacy_prompt and turn_reports:
            first_turn = turn_reports[0]
            report.update(
                {
                    "prompt": first_turn.get("prompt"),
                    "response_status": first_turn.get("response_status"),
                    "response": first_turn.get("response"),
                }
            )
        return _SampleOutcome(
            status=status,
            report=report,
            latencies=tuple(latencies),
            token_total=token_total,
            tool_violation=tool_violation,
            timed_out=timed_out,
            turn_count=turn_count,
            behavior_signature=(
                tuple(decision_sequence),
                tuple(actual_tool_sequence),
            ),
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
        latencies: tuple[float, ...] = (),
        token_total: int = 0,
        tool_violation: bool = False,
        timed_out: bool = False,
        turn_count: int = 0,
        sample_count: int = 0,
        sample_passed: int = 0,
        consistency_numerator: int = 0,
        consistency_denominator: int = 0,
    ) -> None:
        self.case = case
        self.report = report
        self.latencies = latencies
        self.token_total = token_total
        self.tool_violation = tool_violation
        self.timed_out = timed_out
        self.turn_count = turn_count
        self.sample_count = sample_count
        self.sample_passed = sample_passed
        self.consistency_numerator = consistency_numerator
        self.consistency_denominator = consistency_denominator


class _SampleOutcome:
    def __init__(
        self,
        *,
        status: str,
        report: dict[str, Any],
        latencies: tuple[float, ...] = (),
        token_total: int = 0,
        tool_violation: bool = False,
        timed_out: bool = False,
        turn_count: int = 0,
        behavior_signature: tuple[tuple[str, ...], tuple[str, ...]] = ((), ()),
    ) -> None:
        self.status = status
        self.report = report
        self.latencies = latencies
        self.token_total = token_total
        self.tool_violation = tool_violation
        self.timed_out = timed_out
        self.turn_count = turn_count
        self.behavior_signature = behavior_signature


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
    expected: AgentExpectation, status_code: int, body: Any
) -> tuple[list[str], bool, int]:
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
    tool_calls = body["tool_calls"]
    actual_tools = {call["name"] for call in tool_calls}
    disallowed_tools = actual_tools - expected.allowed_tools
    missing_tools = expected.required_tools - actual_tools
    tool_violation = bool(disallowed_tools)
    if disallowed_tools:
        reasons.append("disallowed tools: " + ", ".join(sorted(disallowed_tools)))
    if missing_tools:
        reasons.append("missing required tools: " + ", ".join(sorted(missing_tools)))
    if expected.max_tool_calls is not None and len(tool_calls) > expected.max_tool_calls:
        reasons.append(
            f"tool call limit exceeded: {len(tool_calls)} > {expected.max_tool_calls}"
        )
        tool_violation = True
    for call_index, call in enumerate(tool_calls, start=1):
        schema = expected.tool_argument_schemas.get(call["name"])
        if schema is None:
            continue
        errors = sorted(
            Draft202012Validator(schema).iter_errors(call["arguments"]),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                str(error.validator),
            ),
        )
        for error in errors:
            path = (
                ".".join(str(part) for part in error.absolute_path) or "$"
            )
            reasons.append(
                f"{call['name']} call {call_index} arguments violate schema "
                f"at {path}: {error.validator}"
            )
            tool_violation = True
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
    turn_count: int,
    sample_count: int,
    sample_passed: int,
    consistency_numerator: int,
    consistency_denominator: int,
) -> tuple[MetricData, ...]:
    total = max(summary.total, 1)
    ordered = sorted(latencies)
    p95_ms = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else 0.0
    return (
        MetricData("agent_pass_rate", summary.passed / total, "ratio"),
        MetricData("tool_violation_rate", tool_violation_count / total, "ratio"),
        MetricData("agent_p95_latency_ms", p95_ms, "ms"),
        MetricData("total_tokens", token_total, "count"),
        MetricData("agent_turn_count", turn_count, "count"),
        MetricData("agent_sample_count", sample_count, "count"),
        MetricData(
            "agent_sample_pass_rate",
            sample_passed / sample_count if sample_count else 0.0,
            "ratio",
        ),
        MetricData(
            "agent_behavior_consistency_rate",
            consistency_numerator / consistency_denominator
            if consistency_denominator
            else 0.0,
            "ratio",
        ),
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


def _turn_error_report(
    turn_index: int,
    prompt: str | None,
    message: str,
    duration_ms: float,
) -> dict[str, Any]:
    return {
        "index": turn_index,
        "status": "error",
        "duration_ms": duration_ms,
        "message": message,
        "prompt": prompt,
    }


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
