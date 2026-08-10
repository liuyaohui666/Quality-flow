"""Safe adapter for registered pytest suites."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
import re
import sys

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.base import (
    ExecutionSpec,
    RunnerArtifact,
    RunnerOutcome,
)
from quality_flow.runners.gates import evaluate_functional_gate
from quality_flow.runners.parsers import ResultParseError, parse_junit_xml
from quality_flow.runners.subprocess_runner import (
    ProcessExecution,
    RunnerConfigurationError,
    SafeSubprocessExecutor,
    UnsafeRunnerResult,
    build_clean_environment,
    prepare_result_directory,
    prepare_staging_directory,
    validate_result_file,
    validate_suite_path,
    validate_workspace,
)


_PYTEST_SAFE_FLAGS = frozenset(
    {
        "-q",
        "-v",
        "-vv",
        "-x",
        "--disable-warnings",
        "--strict-markers",
        "--strict-config",
    }
)
_PYTEST_RESERVED_PREFIXES = (
    "--junitxml",
    "--junit-xml",
    "--capture",
    "--basetemp",
    "--rootdir",
    "--confcutdir",
    "--override-ini",
)
_SAFE_EXPRESSION = re.compile(r"^[A-Za-z0-9_ ().-]+$")


class PytestRunner:
    def __init__(
        self,
        *,
        poll_interval_seconds: float = 0.05,
        max_stream_bytes: int = 1024 * 1024,
        max_total_output_bytes: int = 2 * 1024 * 1024,
        environment: Mapping[str, str] | None = None,
        staging_root: Path | None = None,
    ) -> None:
        self._executor = SafeSubprocessExecutor(
            poll_interval_seconds=poll_interval_seconds,
            max_stream_bytes=max_stream_bytes,
            max_total_output_bytes=max_total_output_bytes,
        )
        self._environment = dict(environment or {})
        self._staging_root = Path(staging_root) if staging_root is not None else None

    def run(
        self,
        spec: ExecutionSpec,
        workspace: Path,
        heartbeat: Callable[[], None],
    ) -> RunnerOutcome:
        resolved_workspace = validate_workspace(
            workspace, spec.allowed_workspace_root
        )
        result_directory = prepare_result_directory(
            resolved_workspace, spec.allowed_workspace_root
        )
        junit_path = result_directory / "junit.xml"
        argv = _build_pytest_argv(spec.argv, resolved_workspace, junit_path)
        execution = self._executor.execute(
            argv,
            workspace=resolved_workspace,
            timeout_seconds=spec.timeout_seconds,
            heartbeat=heartbeat,
            result_directory=result_directory,
            allowed_workspace_root=spec.allowed_workspace_root,
            environment=build_clean_environment(spec.parameters, self._environment),
        )
        staging_directory = prepare_staging_directory(
            resolved_workspace,
            staging_parent=self._staging_root,
        )
        artifacts, artifact_error = _log_artifacts(
            execution,
            resolved_workspace,
            staging_directory,
        )

        if execution.timed_out:
            return _outcome(
                execution,
                AttemptStatus.TIMED_OUT,
                artifacts=artifacts,
                failure_kind="timeout",
                failure_summary="pytest exceeded its configured timeout",
            )
        if execution.output_limit_exceeded:
            return _outcome(
                execution,
                AttemptStatus.INFRA_FAILED,
                artifacts=artifacts,
                failure_kind="output_limit_exceeded",
                failure_summary="pytest output exceeded its configured byte limit",
            )
        if execution.infrastructure_error or artifact_error:
            return _outcome(
                execution,
                AttemptStatus.INFRA_FAILED,
                artifacts=artifacts,
                failure_kind=execution.infrastructure_error or "unsafe_artifact",
                failure_summary="pytest process output could not be captured safely",
            )

        try:
            safe_junit_path = validate_result_file(
                junit_path,
                resolved_workspace,
                staging_directory,
            )
            parsed = parse_junit_xml(safe_junit_path)
        except (ResultParseError, UnsafeRunnerResult):
            return _outcome(
                execution,
                AttemptStatus.INFRA_FAILED,
                artifacts=artifacts,
                failure_kind="invalid_result",
                failure_summary="pytest did not produce a trustworthy JUnit report",
            )

        artifacts += (
            RunnerArtifact(
                "junit_xml",
                safe_junit_path,
                staging_directory,
                "application/xml",
            ),
        )
        gate_result = evaluate_functional_gate(parsed.summary, spec.gate_policy)
        if execution.exit_code == 0:
            if parsed.summary.total == 0 or parsed.summary.failed or parsed.summary.errors:
                status = AttemptStatus.INFRA_FAILED
                failure_kind = "inconsistent_result"
                failure_summary = "pytest exit code conflicts with its JUnit report"
            elif not gate_result.passed:
                status = AttemptStatus.TEST_FAILED
                failure_kind = "quality_gate_failed"
                failure_summary = "Functional gate failed: " + ", ".join(
                    gate_result.reason_codes
                )
            else:
                status = AttemptStatus.PASSED
                failure_kind = None
                failure_summary = None
        elif execution.exit_code == 1 and (
            parsed.summary.failed or parsed.summary.errors
        ):
            status = AttemptStatus.TEST_FAILED
            failure_kind = "test_failure"
            failure_summary = "one or more pytest cases failed"
        else:
            status = AttemptStatus.INFRA_FAILED
            failure_kind = "pytest_internal_error"
            failure_summary = "pytest did not finish with a supported test result"
        return _outcome(
            execution,
            status,
            artifacts=artifacts,
            case_results=parsed.cases,
            case_summary=parsed.summary,
            gate_result=(
                gate_result if execution.exit_code in {0, 1} else None
            ),
            failure_kind=failure_kind,
            failure_summary=failure_summary,
        )


def _build_pytest_argv(
    configured_argv: tuple[str, ...], workspace: Path, junit_path: Path
) -> list[str]:
    if len(configured_argv) < 4 or not _python_module_prefix(
        configured_argv, "pytest"
    ):
        raise RunnerConfigurationError(
            "pytest argv must start with python -m pytest and include a target"
        )
    arguments = list(configured_argv[3:])
    safe_arguments: list[str] = []
    target_count = 0
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-c", "-s"} or argument.startswith(
            _PYTEST_RESERVED_PREFIXES
        ):
            raise RunnerConfigurationError("pytest argument is reserved by the runner")
        if argument in _PYTEST_SAFE_FLAGS or re.fullmatch(
            r"--(?:maxfail|tb)=[A-Za-z0-9_-]+", argument
        ):
            safe_arguments.append(argument)
            index += 1
            continue
        if argument in {"-k", "-m"}:
            if index + 1 >= len(arguments) or not _SAFE_EXPRESSION.fullmatch(
                arguments[index + 1]
            ):
                raise RunnerConfigurationError("pytest expression is unsafe")
            safe_arguments.extend((argument, arguments[index + 1]))
            index += 2
            continue
        if argument.startswith("-"):
            raise RunnerConfigurationError("pytest argument is not allowlisted")
        target_path = argument.split("::", 1)[0]
        validate_suite_path(target_path, workspace)
        safe_arguments.append(argument)
        target_count += 1
        index += 1
    if target_count == 0:
        raise RunnerConfigurationError("pytest argv must include a workspace target")
    relative_junit = junit_path.relative_to(workspace)
    return [
        sys.executable,
        "-m",
        "pytest",
        *safe_arguments,
        "--capture=no",
        f"--junitxml={relative_junit}",
    ]


def _python_module_prefix(argv: tuple[str, ...], module: str) -> bool:
    if len(argv) < 3:
        return False
    executable_name = Path(argv[0]).name.casefold()
    return (
        executable_name in {"python", "python.exe", "python3", "python3.exe"}
        and argv[1:3] == ("-m", module)
    )


def _log_artifacts(
    execution: ProcessExecution,
    workspace: Path,
    staging_directory: Path,
) -> tuple[tuple[RunnerArtifact, ...], bool]:
    artifacts: list[RunnerArtifact] = []
    unsafe = False
    for artifact_type, path in (
        ("stdout", execution.stdout_path),
        ("stderr", execution.stderr_path),
    ):
        try:
            safe_path = validate_result_file(path, workspace, staging_directory)
        except UnsafeRunnerResult:
            unsafe = True
            continue
        artifacts.append(
            RunnerArtifact(
                artifact_type,
                safe_path,
                staging_directory,
                "text/plain",
            )
        )
    return tuple(artifacts), unsafe


def _outcome(
    execution: ProcessExecution,
    status: AttemptStatus,
    *,
    artifacts: tuple[RunnerArtifact, ...],
    case_results=(),
    case_summary=None,
    gate_result=None,
    failure_kind: str | None = None,
    failure_summary: str | None = None,
) -> RunnerOutcome:
    return RunnerOutcome(
        attempt_status=status,
        exit_code=execution.exit_code,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        case_results=case_results,
        case_summary=case_summary,
        gate_result=gate_result,
        artifacts=artifacts,
        failure_kind=failure_kind,
        failure_summary=failure_summary,
    )
