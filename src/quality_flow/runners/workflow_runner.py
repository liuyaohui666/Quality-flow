"""Bounded execution of trusted declarative HTTP workflows."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.base import (
    CaseResultData,
    CaseSummary,
    ExecutionSpec,
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
    MissingTemplateVariable,
    WorkflowDefinition,
    WorkflowDefinitionError,
    WorkflowStep,
    extract_json_path,
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


class WorkflowRunner:
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
            definition_path = _definition_path(spec.argv, resolved_workspace)
            definition = WorkflowDefinition.from_yaml(definition_path)
            context: dict[str, Any] = {
                "request": dict(spec.request_body or {}),
                "parameters": dict(spec.parameters),
                "env": dict(self._environment),
            }
            rendered_base_url = render_template(definition.base_url, context)
            base_url = _validated_base_url(rendered_base_url)
        except (RunnerConfigurationError, WorkflowDefinitionError, TypeError, ValueError) as error:
            return RunnerOutcome(
                attempt_status=AttemptStatus.INFRA_FAILED,
                exit_code=None,
                started_at=started_at,
                finished_at=self._clock(),
                failure_kind="workflow_configuration",
                failure_summary=f"workflow configuration is invalid: {error}",
            )

        cases: list[CaseResultData] = []
        reports: list[dict[str, Any]] = []
        captured_names: set[str] = set()
        main_failed = False
        timed_out = False
        request_error = False

        with httpx.Client(
            base_url=base_url,
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            for step in definition.steps:
                if main_failed:
                    cases.append(_skipped_case(step, "previous workflow step failed"))
                    reports.append(_skipped_report(step, "main", "previous workflow step failed"))
                    continue
                outcome = self._run_step(
                    step,
                    phase="main",
                    client=client,
                    definition=definition,
                    context=context,
                    deadline=deadline,
                    heartbeat=heartbeat,
                )
                cases.append(outcome.case)
                reports.append(outcome.report)
                context.update(outcome.captured)
                captured_names.update(outcome.captured)
                if outcome.timed_out:
                    timed_out = True
                    main_failed = True
                elif outcome.case.status in {"failed", "error"}:
                    request_error = request_error or outcome.case.status == "error"
                    main_failed = True

            for step in definition.cleanup:
                missing = [name for name in step.when_variables if name not in context]
                if missing:
                    reason = "missing variables: " + ", ".join(missing)
                    cases.append(_skipped_case(step, reason))
                    reports.append(_skipped_report(step, "cleanup", reason))
                    continue
                if self._monotonic() >= deadline:
                    timed_out = True
                    reason = "run deadline exceeded before cleanup"
                    cases.append(_skipped_case(step, reason))
                    reports.append(_skipped_report(step, "cleanup", reason))
                    continue
                outcome = self._run_step(
                    step,
                    phase="cleanup",
                    client=client,
                    definition=definition,
                    context=context,
                    deadline=deadline,
                    heartbeat=heartbeat,
                )
                cases.append(outcome.case)
                reports.append(outcome.report)
                context.update(outcome.captured)
                captured_names.update(outcome.captured)
                timed_out = timed_out or outcome.timed_out
                request_error = request_error or outcome.case.status == "error"

        summary = _case_summary(cases)
        gate = evaluate_functional_gate(summary, spec.gate_policy)
        finished_at = self._clock()
        try:
            artifact = self._write_report(
                resolved_workspace,
                definition,
                reports,
                captured_names,
                started_at,
                finished_at,
            )
            artifacts = (artifact,)
        except (OSError, RunnerConfigurationError, TypeError, ValueError) as error:
            return RunnerOutcome(
                attempt_status=AttemptStatus.INFRA_FAILED,
                exit_code=None,
                started_at=started_at,
                finished_at=finished_at,
                case_results=tuple(cases),
                case_summary=summary,
                gate_result=gate,
                failure_kind="workflow_report",
                failure_summary=f"workflow report could not be written: {error}",
            )

        if timed_out:
            status = AttemptStatus.TIMED_OUT
            failure_kind = "timeout"
            failure_summary = "workflow exceeded its configured Run timeout"
            exit_code = None
        elif any(case.status in {"failed", "error"} for case in cases) or not gate.passed:
            status = AttemptStatus.TEST_FAILED
            failure_kind = "workflow_request_error" if request_error else "workflow_assertion"
            failure_summary = "one or more workflow steps did not meet expectations"
            exit_code = 1
        else:
            status = AttemptStatus.PASSED
            failure_kind = None
            failure_summary = None
            exit_code = 0
        return RunnerOutcome(
            attempt_status=status,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            case_results=tuple(cases),
            case_summary=summary,
            gate_result=gate,
            artifacts=artifacts,
            failure_kind=failure_kind,
            failure_summary=failure_summary,
        )

    def _run_step(
        self,
        step: WorkflowStep,
        *,
        phase: str,
        client: httpx.Client,
        definition: WorkflowDefinition,
        context: dict[str, Any],
        deadline: float,
        heartbeat: Callable[[], None],
    ) -> "_StepOutcome":
        step_started = self._monotonic()
        deadline_limited_request = False
        if step_started >= deadline:
            message = "run deadline exceeded before request"
            return _StepOutcome(
                case=CaseResultData(
                    node_id=f"workflow::{step.step_id}",
                    status="error",
                    duration_ms=0.0,
                    message=message,
                ),
                report=_error_report(step, phase, message, 0.0),
                timed_out=True,
            )
        try:
            path = render_template(step.request.path, context)
            headers = render_template(step.request.headers, context)
            query = render_template(step.request.query, context)
            body = render_template(step.request.json, context)
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
                raise WorkflowDefinitionError("request path must be relative and start with /")
            heartbeat()
            remaining = max(deadline - self._monotonic(), 0.001)
            deadline_limited_request = remaining <= definition.request_timeout_seconds
            response = client.request(
                step.request.method,
                path,
                headers=headers,
                params=query,
                json=body,
                timeout=min(definition.request_timeout_seconds, remaining),
            )
            response_body = _response_body(response)
            assertion_error = _assert_response(step, response, response_body, context)
            captured: dict[str, Any] = {}
            if assertion_error is None and step.capture:
                if not isinstance(response_body, (dict, list)):
                    assertion_error = "capture requires a JSON response"
                else:
                    try:
                        captured = {
                            name: extract_json_path(response_body, path)
                            for name, path in step.capture.items()
                        }
                    except WorkflowDefinitionError as error:
                        assertion_error = str(error)
            duration_ms = (self._monotonic() - step_started) * 1000
            status = "passed" if assertion_error is None else "failed"
            report = {
                "id": step.step_id,
                "name": step.name,
                "phase": phase,
                "status": status,
                "duration_ms": duration_ms,
                "message": assertion_error,
                "request": {
                    "method": step.request.method,
                    "path": path,
                    "headers": _sanitize(headers),
                    "query": _sanitize(query),
                    "body": _bounded_body(_sanitize(body)),
                },
                "response": {
                    "status": response.status_code,
                    "body": _bounded_body(_sanitize(response_body)),
                },
                "captured": sorted(captured),
            }
            return _StepOutcome(
                case=CaseResultData(
                    node_id=f"workflow::{step.step_id}",
                    status=status,
                    duration_ms=duration_ms,
                    message=assertion_error,
                ),
                report=report,
                captured=captured,
            )
        except MissingTemplateVariable as error:
            duration_ms = (self._monotonic() - step_started) * 1000
            return _StepOutcome(
                case=CaseResultData(
                    node_id=f"workflow::{step.step_id}",
                    status="error",
                    duration_ms=duration_ms,
                    message=str(error),
                ),
                report=_error_report(step, phase, str(error), duration_ms),
            )
        except httpx.TimeoutException as error:
            duration_ms = (self._monotonic() - step_started) * 1000
            if deadline_limited_request:
                message = "run deadline exceeded during HTTP request"
                return _StepOutcome(
                    case=CaseResultData(
                        node_id=f"workflow::{step.step_id}",
                        status="error",
                        duration_ms=duration_ms,
                        message=message,
                    ),
                    report=_error_report(step, phase, message, duration_ms),
                    timed_out=True,
                )
            message = f"HTTP request timed out: {error}"
            return _StepOutcome(
                case=CaseResultData(
                    node_id=f"workflow::{step.step_id}",
                    status="error",
                    duration_ms=duration_ms,
                    message=message,
                ),
                report=_error_report(step, phase, message, duration_ms),
            )
        except httpx.HTTPError as error:
            duration_ms = (self._monotonic() - step_started) * 1000
            message = f"HTTP request failed: {error}"
            return _StepOutcome(
                case=CaseResultData(
                    node_id=f"workflow::{step.step_id}",
                    status="error",
                    duration_ms=duration_ms,
                    message=message,
                ),
                report=_error_report(step, phase, message, duration_ms),
            )
        except (TypeError, ValueError, WorkflowDefinitionError) as error:
            duration_ms = (self._monotonic() - step_started) * 1000
            message = f"workflow step could not be rendered: {error}"
            return _StepOutcome(
                case=CaseResultData(
                    node_id=f"workflow::{step.step_id}",
                    status="error",
                    duration_ms=duration_ms,
                    message=message,
                ),
                report=_error_report(step, phase, message, duration_ms),
            )

    def _write_report(
        self,
        workspace: Path,
        definition: WorkflowDefinition,
        steps: list[dict[str, Any]],
        captured_names: set[str],
        started_at: datetime,
        finished_at: datetime,
    ) -> RunnerArtifact:
        staging = prepare_staging_directory(
            workspace, staging_parent=self._staging_root
        )
        report_path = staging / "workflow-report.json"
        report_path.write_text(
            json.dumps(
                {
                    "workflow": definition.name,
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "captured_variables": sorted(captured_names),
                    "steps": steps,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return RunnerArtifact(
            artifact_type="workflow_report",
            source_path=report_path,
            source_root=staging,
            mime_type="application/json",
        )


class _StepOutcome:
    def __init__(
        self,
        *,
        case: CaseResultData,
        report: dict[str, Any],
        captured: Mapping[str, Any] | None = None,
        timed_out: bool = False,
    ) -> None:
        self.case = case
        self.report = report
        self.captured = dict(captured or {})
        self.timed_out = timed_out


def _definition_path(argv: tuple[str, ...], workspace: Path) -> Path:
    if len(argv) != 2 or argv[0] != "workflow":
        raise RunnerConfigurationError(
            "workflow argv must be exactly: workflow <relative-yaml-path>"
        )
    if Path(argv[1]).suffix.casefold() not in {".yaml", ".yml"}:
        raise RunnerConfigurationError("workflow definition must be YAML")
    return validate_suite_path(argv[1], workspace, regular_file=True)


def _validated_base_url(value: Any) -> str:
    if not isinstance(value, str):
        raise WorkflowDefinitionError("workflow base_url must render to text")
    url = httpx.URL(value)
    if url.scheme not in {"http", "https"} or not url.host:
        raise WorkflowDefinitionError("workflow base_url must be an HTTP(S) origin")
    if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        raise WorkflowDefinitionError(
            "workflow base_url must be an origin without credentials, path, query, or fragment"
        )
    return str(url)


def _assert_response(
    step: WorkflowStep,
    response: httpx.Response,
    response_body: Any,
    context: Mapping[str, Any],
) -> str | None:
    if response.status_code != step.expect.status:
        return f"expected status {step.expect.status}, got {response.status_code}"
    if step.expect.json_contains is not None:
        expected = render_template(step.expect.json_contains, context)
        if not _contains(response_body, expected):
            return "response JSON does not contain the expected values"
    if step.expect.json_schema is not None:
        schema = render_template(step.expect.json_schema, context)
        try:
            Draft202012Validator.check_schema(schema)
            errors = sorted(
                Draft202012Validator(schema).iter_errors(response_body),
                key=lambda error: list(error.absolute_path),
            )
        except SchemaError as error:
            raise WorkflowDefinitionError("response JSON Schema is invalid") from error
        if errors:
            return f"response JSON Schema failed: {errors[0].message}"
    return None


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            _contains(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    return actual == expected


def _response_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "").casefold()
    if "json" in content_type:
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeError):
            return response.text
    return response.text


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if key is not None and any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): _sanitize(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _bounded_body(value: Any) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return "[UNSERIALIZABLE]"
    if len(encoded) <= _MAX_REPORT_BODY_BYTES:
        return value
    return f"[TRUNCATED body exceeded {_MAX_REPORT_BODY_BYTES} bytes]"


def _case_summary(cases: list[CaseResultData]) -> CaseSummary:
    return CaseSummary(
        total=len(cases),
        passed=sum(case.status == "passed" for case in cases),
        failed=sum(case.status == "failed" for case in cases),
        errors=sum(case.status == "error" for case in cases),
        skipped=sum(case.status == "skipped" for case in cases),
    )


def _skipped_case(step: WorkflowStep, reason: str) -> CaseResultData:
    return CaseResultData(
        node_id=f"workflow::{step.step_id}",
        status="skipped",
        duration_ms=0.0,
        message=reason,
    )


def _skipped_report(step: WorkflowStep, phase: str, reason: str) -> dict[str, Any]:
    return {
        "id": step.step_id,
        "name": step.name,
        "phase": phase,
        "status": "skipped",
        "duration_ms": 0.0,
        "message": reason,
    }


def _error_report(
    step: WorkflowStep, phase: str, message: str, duration_ms: float
) -> dict[str, Any]:
    return {
        "id": step.step_id,
        "name": step.name,
        "phase": phase,
        "status": "error",
        "duration_ms": duration_ms,
        "message": message,
    }
