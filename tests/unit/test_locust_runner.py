from __future__ import annotations

from pathlib import Path
import os
import stat
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.base import ExecutionSpec
from quality_flow.runners.locust_runner import LocustRunner
from quality_flow.runners import subprocess_runner as subprocess_module
from quality_flow.runners.parsers import ResultParseError
from quality_flow.runners.subprocess_runner import RunnerConfigurationError
from quality_flow.suites.registry import GatePolicy


LOCUSTFILE = """\
import os
from pathlib import Path

from locust import User, between, events, task

Path("locust.pid").write_text(str(os.getpid()), encoding="ascii")


class SyntheticUser(User):
    wait_time = between(0.001, 0.002)

    @task
    def work(self):
        scenario = os.environ.get("QUALITY_FLOW_PARAM_SCENARIO", "normal")
        response_time = 900 if scenario == "degraded" else 20
        exception = RuntimeError("synthetic failure") if scenario == "failure" else None
        events.request.fire(
            request_type="GET",
            name="/work",
            response_time=response_time,
            response_length=2,
            exception=exception,
        )
"""


def _write_locustfile(workspace: Path) -> None:
    (workspace / "locustfile.py").write_text(LOCUSTFILE, encoding="utf-8")


def _wait_until_stopped(pid: int, timeout_seconds: float = 5) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.Error:
            return
        time.sleep(0.05)
    pytest.fail(f"Locust process {pid} survived runner cleanup")


def _spec(
    allowed_workspace_root: Path,
    scenario: str,
    *,
    timeout_seconds: float = 10,
    run_time: str = "2s",
    gate_policy: GatePolicy | None = None,
    extra_argv: tuple[str, ...] = (),
) -> ExecutionSpec:
    return ExecutionSpec(
        argv=(
            "python",
            "-m",
            "locust",
            "-f",
            "locustfile.py",
            "--users",
            "1",
            "--spawn-rate",
            "10",
            "--run-time",
            run_time,
            *extra_argv,
        ),
        timeout_seconds=timeout_seconds,
        allowed_workspace_root=allowed_workspace_root,
        parameters={"scenario": scenario},
        gate_policy=gate_policy
        or GatePolicy(max_error_rate=0.05, max_p95_ms=500, min_requests=1),
    )


def test_locust_runner_returns_metrics_and_artifacts_for_normal_load(
    tmp_path: Path,
) -> None:
    _write_locustfile(tmp_path)

    outcome = LocustRunner().run(_spec(tmp_path, "normal"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.PASSED
    assert outcome.exit_code == 0
    assert outcome.case_results == ()
    assert outcome.case_summary is None
    assert outcome.performance_summary is not None
    assert outcome.performance_summary.request_count > 0
    assert outcome.performance_summary.failure_ratio == 0
    assert outcome.performance_summary.p95_ms < 500
    assert outcome.gate_result is not None
    assert outcome.gate_result.passed is True
    assert {artifact.artifact_type for artifact in outcome.artifacts} == {
        "locust_stats",
        "stdout",
        "stderr",
    }
    source_roots = {artifact.source_root for artifact in outcome.artifacts}
    assert len(source_roots) == 1
    source_root = source_roots.pop()
    assert source_root.is_dir()
    assert not source_root.is_relative_to(tmp_path.resolve())
    assert all(
        artifact.source_path.is_relative_to(source_root)
        for artifact in outcome.artifacts
    )


def test_locust_runner_classifies_p95_regression_as_test_failure(tmp_path: Path) -> None:
    _write_locustfile(tmp_path)

    outcome = LocustRunner().run(_spec(tmp_path, "degraded"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.TEST_FAILED
    assert outcome.performance_summary is not None
    assert outcome.performance_summary.p95_ms > 500
    assert outcome.failure_kind == "quality_gate_failed"
    assert outcome.gate_result is not None
    assert "p95_ms" in outcome.gate_result.reason_codes


def test_locust_runner_classifies_excess_request_failures_as_test_failure(
    tmp_path: Path,
) -> None:
    _write_locustfile(tmp_path)

    outcome = LocustRunner().run(_spec(tmp_path, "failure"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.TEST_FAILED
    assert outcome.performance_summary is not None
    assert outcome.performance_summary.failure_ratio > 0.05
    assert outcome.failure_kind == "quality_gate_failed"


def test_locust_runner_times_out_and_reaps_process(tmp_path: Path) -> None:
    _write_locustfile(tmp_path)

    outcome = LocustRunner(poll_interval_seconds=0.02).run(
        _spec(tmp_path, "normal", run_time="1m", timeout_seconds=2),
        tmp_path,
        lambda: None,
    )

    assert outcome.attempt_status is AttemptStatus.TIMED_OUT
    assert outcome.failure_kind == "timeout"
    locust_pid = int((tmp_path / "locust.pid").read_text(encoding="ascii"))
    _wait_until_stopped(locust_pid)


def test_locust_runner_calls_heartbeat(tmp_path: Path) -> None:
    _write_locustfile(tmp_path)
    beats: list[object] = []

    outcome = LocustRunner(poll_interval_seconds=0.02).run(
        _spec(tmp_path, "normal"), tmp_path, lambda: beats.append(object())
    )

    assert outcome.attempt_status is AttemptStatus.PASSED
    assert beats


def test_locust_runner_maps_invalid_csv_to_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_locustfile(tmp_path)

    def reject_csv(path: Path):
        raise ResultParseError(f"invalid CSV at {path.name}")

    monkeypatch.setattr(
        "quality_flow.runners.locust_runner.parse_locust_stats", reject_csv
    )

    outcome = LocustRunner().run(_spec(tmp_path, "normal"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.INFRA_FAILED
    assert outcome.failure_kind == "invalid_result"


def test_locust_runner_maps_reparse_csv_to_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_locustfile(tmp_path)
    real_lstat = os.lstat

    def lstat_with_reparse_stats(path: object):
        if Path(path).name.endswith("_stats.csv"):
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        return real_lstat(path)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess_module.os, "lstat", lstat_with_reparse_stats)

    outcome = LocustRunner().run(_spec(tmp_path, "normal"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.INFRA_FAILED
    assert outcome.failure_kind == "invalid_result"


@pytest.mark.parametrize(
    "reserved_arguments",
    [
        ("--headless",),
        ("--csv=outside",),
        ("--csv", "outside"),
        ("--exit-code-on-error=7",),
    ],
)
def test_locust_runner_rejects_reserved_output_arguments(
    tmp_path: Path, reserved_arguments: tuple[str, ...]
) -> None:
    _write_locustfile(tmp_path)

    with pytest.raises(RunnerConfigurationError, match="reserved"):
        LocustRunner().run(
            _spec(tmp_path, "normal", extra_argv=reserved_arguments),
            tmp_path,
            lambda: None,
        )


def test_locust_runner_rejects_locustfile_outside_workspace(tmp_path: Path) -> None:
    with pytest.raises(RunnerConfigurationError):
        LocustRunner().run(
            _spec(tmp_path, "normal").__class__(
                argv=(sys.executable, "-m", "locust", "-f", "../locustfile.py"),
                timeout_seconds=10,
                allowed_workspace_root=tmp_path,
                parameters={"scenario": "normal"},
                gate_policy=GatePolicy(),
            ),
            tmp_path,
            lambda: None,
        )
