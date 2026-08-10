"""Safe adapter for registered single-process Locust suites."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
from pathlib import Path
import re
import sys

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.base import ExecutionSpec, RunnerArtifact, RunnerOutcome
from quality_flow.runners.gates import evaluate_performance_gate
from quality_flow.runners.parsers import ResultParseError, parse_locust_stats
from quality_flow.runners.pytest_runner import _log_artifacts
from quality_flow.runners.subprocess_runner import (
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


_LOCUST_RESERVED_PREFIXES = (
    "--headless",
    "--csv",
    "--exit-code-on-error",
    "--processes",
    "--worker",
    "--master",
)


class LocustRunner:
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
        csv_prefix = result_directory / "locust"
        argv = _build_locust_argv(spec.argv, resolved_workspace, csv_prefix)
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
                artifacts,
                "timeout",
                "Locust exceeded its configured timeout",
            )
        if execution.output_limit_exceeded:
            return _outcome(
                execution,
                AttemptStatus.INFRA_FAILED,
                artifacts,
                "output_limit_exceeded",
                "Locust output exceeded its configured byte limit",
            )
        if execution.infrastructure_error or artifact_error:
            return _outcome(
                execution,
                AttemptStatus.INFRA_FAILED,
                artifacts,
                execution.infrastructure_error or "unsafe_artifact",
                "Locust process output could not be captured safely",
            )

        stats_path = Path(f"{csv_prefix}_stats.csv")
        try:
            safe_stats_path = validate_result_file(
                stats_path,
                resolved_workspace,
                staging_directory,
            )
            performance = parse_locust_stats(safe_stats_path)
        except (ResultParseError, UnsafeRunnerResult):
            return _outcome(
                execution,
                AttemptStatus.INFRA_FAILED,
                artifacts,
                "invalid_result",
                "Locust did not produce a trustworthy aggregate CSV",
            )

        artifacts += (
            RunnerArtifact(
                "locust_stats",
                safe_stats_path,
                staging_directory,
                "text/csv",
            ),
        )
        if execution.exit_code != 0:
            return _outcome(
                execution,
                AttemptStatus.INFRA_FAILED,
                artifacts,
                "locust_internal_error",
                "Locust did not finish successfully",
                performance,
            )
        gate = evaluate_performance_gate(performance, spec.gate_policy)
        if gate.passed:
            return _outcome(
                execution,
                AttemptStatus.PASSED,
                artifacts,
                None,
                None,
                performance,
                gate,
            )
        return _outcome(
            execution,
            AttemptStatus.TEST_FAILED,
            artifacts,
            "quality_gate_failed",
            "Performance gate failed: " + ", ".join(gate.reason_codes),
            performance,
            gate,
        )


def _build_locust_argv(
    configured_argv: tuple[str, ...], workspace: Path, csv_prefix: Path
) -> list[str]:
    if len(configured_argv) < 5 or not _python_module_prefix(
        configured_argv, "locust"
    ):
        raise RunnerConfigurationError(
            "Locust argv must start with python -m locust and include -f"
        )
    arguments = list(configured_argv[3:])
    safe_arguments: list[str] = []
    locustfile_seen = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument.startswith(_LOCUST_RESERVED_PREFIXES):
            raise RunnerConfigurationError("Locust argument is reserved by the runner")
        if argument in {"-f", "--locustfile"}:
            if locustfile_seen or index + 1 >= len(arguments):
                raise RunnerConfigurationError("Locust needs one locustfile")
            locustfile = arguments[index + 1]
            validate_suite_path(locustfile, workspace, regular_file=True)
            safe_arguments.extend((argument, locustfile))
            locustfile_seen = True
            index += 2
            continue
        if argument in {"-u", "--users"}:
            value = _required_value(arguments, index, argument)
            if not value.isdigit() or int(value) <= 0:
                raise RunnerConfigurationError("Locust users must be a positive integer")
            safe_arguments.extend((argument, value))
            index += 2
            continue
        if argument in {"-r", "--spawn-rate"}:
            value = _required_value(arguments, index, argument)
            try:
                numeric_value = float(value)
            except ValueError as error:
                raise RunnerConfigurationError("Locust spawn rate must be positive") from error
            if not math.isfinite(numeric_value) or numeric_value <= 0:
                raise RunnerConfigurationError("Locust spawn rate must be positive")
            safe_arguments.extend((argument, value))
            index += 2
            continue
        if argument in {"-t", "--run-time"}:
            value = _required_value(arguments, index, argument)
            if not re.fullmatch(r"[1-9][0-9]*[smh]", value):
                raise RunnerConfigurationError("Locust run time must use Ns, Nm, or Nh")
            safe_arguments.extend((argument, value))
            index += 2
            continue
        if argument == "--only-summary":
            safe_arguments.append(argument)
            index += 1
            continue
        raise RunnerConfigurationError("Locust argument is not allowlisted")
    if not locustfile_seen:
        raise RunnerConfigurationError("Locust argv must include one locustfile")

    relative_prefix = csv_prefix.relative_to(workspace)
    return [
        sys.executable,
        "-m",
        "locust",
        *safe_arguments,
        "--headless",
        "--only-summary",
        "--csv",
        str(relative_prefix),
        "--exit-code-on-error",
        "0",
    ]


def _python_module_prefix(argv: tuple[str, ...], module: str) -> bool:
    if len(argv) < 3:
        return False
    executable_name = Path(argv[0]).name.casefold()
    return (
        executable_name in {"python", "python.exe", "python3", "python3.exe"}
        and argv[1:3] == ("-m", module)
    )


def _required_value(arguments: list[str], index: int, option: str) -> str:
    if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
        raise RunnerConfigurationError(f"{option} needs a value")
    return arguments[index + 1]


def _outcome(
    execution,
    status: AttemptStatus,
    artifacts: tuple[RunnerArtifact, ...],
    failure_kind: str | None,
    failure_summary: str | None,
    performance_summary=None,
    gate_result=None,
) -> RunnerOutcome:
    return RunnerOutcome(
        attempt_status=status,
        exit_code=execution.exit_code,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        performance_summary=performance_summary,
        gate_result=gate_result,
        artifacts=artifacts,
        failure_kind=failure_kind,
        failure_summary=failure_summary,
    )
