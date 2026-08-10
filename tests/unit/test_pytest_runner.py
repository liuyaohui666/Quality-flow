from __future__ import annotations

import os
from pathlib import Path
import stat
import time
from types import SimpleNamespace

import psutil
import pytest

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.base import ExecutionSpec
from quality_flow.runners import pytest_runner as pytest_runner_module
from quality_flow.runners.pytest_runner import PytestRunner
from quality_flow.runners import subprocess_runner as subprocess_module
from quality_flow.runners.subprocess_runner import RunnerConfigurationError
from quality_flow.suites.registry import GatePolicy


def _write(workspace: Path, relative_path: str, content: str) -> Path:
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _spec(
    allowed_workspace_root: Path,
    *targets: str,
    timeout_seconds: float = 10,
    parameters: dict[str, str] | None = None,
    gate_policy: GatePolicy | None = None,
) -> ExecutionSpec:
    return ExecutionSpec(
        argv=("python", "-m", "pytest", *targets),
        timeout_seconds=timeout_seconds,
        allowed_workspace_root=allowed_workspace_root,
        parameters=parameters or {},
        gate_policy=gate_policy or GatePolicy(),
    )


def _artifact_path(outcome, artifact_type: str) -> Path:
    return next(
        artifact.source_path
        for artifact in outcome.artifacts
        if artifact.artifact_type == artifact_type
    )


def _process_is_running(pid: int) -> bool:
    if not psutil.pid_exists(pid):
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _wait_until_stopped(pid: int, timeout_seconds: float = 5) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and _process_is_running(pid):
        time.sleep(0.05)
    assert not _process_is_running(pid), f"process {pid} survived runner cleanup"


def _kill_if_running(pid: int) -> None:
    try:
        process = psutil.Process(pid)
        for child in process.children(recursive=True):
            child.kill()
        process.kill()
    except psutil.Error:
        pass


def test_pytest_runner_returns_cases_and_artifact_sources_for_passing_suite(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "test_sample.py", "def test_ok():\n    assert 2 + 2 == 4\n")

    outcome = PytestRunner().run(_spec(tmp_path, "test_sample.py"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.PASSED
    assert outcome.exit_code == 0
    assert outcome.case_summary is not None
    assert outcome.case_summary.total == 1
    assert outcome.case_summary.passed == 1
    assert len(outcome.case_results) == 1
    assert outcome.performance_summary is None
    assert {artifact.artifact_type for artifact in outcome.artifacts} == {
        "junit_xml",
        "stdout",
        "stderr",
    }
    source_roots = {artifact.source_root for artifact in outcome.artifacts}
    assert len(source_roots) == 1
    source_root = source_roots.pop()
    assert source_root.is_dir()
    assert not source_root.is_relative_to(tmp_path.resolve())
    assert all(
        artifact.source_path.is_file()
        and artifact.source_path.is_relative_to(source_root)
        for artifact in outcome.artifacts
    )


def test_pytest_runner_parses_private_snapshot_after_source_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "test_sample.py", "def test_ok():\n    assert True\n")
    real_validate = pytest_runner_module.validate_result_file

    def snapshot_then_replace_source(
        path: Path, workspace: Path, staging_directory: Path, **kwargs
    ) -> Path:
        snapshot = real_validate(path, workspace, staging_directory, **kwargs)
        if path.name == "junit.xml":
            path.write_text("<forged>", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(
        pytest_runner_module, "validate_result_file", snapshot_then_replace_source
    )

    outcome = PytestRunner().run(
        _spec(tmp_path, "test_sample.py"), tmp_path, lambda: None
    )

    assert outcome.attempt_status is AttemptStatus.PASSED
    assert outcome.case_summary is not None
    assert outcome.case_summary.passed == 1


def test_pytest_runner_classifies_assertion_failure_as_test_failure(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "test_sample.py", "def test_no():\n    assert 1 == 2\n")

    outcome = PytestRunner().run(_spec(tmp_path, "test_sample.py"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.TEST_FAILED
    assert outcome.exit_code == 1
    assert outcome.case_summary is not None
    assert outcome.case_summary.failed == 1


def test_pytest_runner_applies_functional_gate_to_skipped_cases(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "test_skipped.py",
        "import pytest\n\n@pytest.mark.skip\ndef test_skipped():\n    pass\n",
    )

    outcome = PytestRunner().run(
        _spec(
            tmp_path,
            "test_skipped.py",
            gate_policy=GatePolicy(min_pass_rate=1.0, max_failures=0),
        ),
        tmp_path,
        lambda: None,
    )

    assert outcome.attempt_status is AttemptStatus.TEST_FAILED
    assert outcome.failure_kind == "quality_gate_failed"
    assert outcome.gate_result is not None
    assert outcome.gate_result.reason_codes == ("pass_rate",)


def test_pytest_runner_classifies_collection_error_as_infrastructure_failure(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "test_broken.py", "def this is not valid python\n")

    outcome = PytestRunner().run(_spec(tmp_path, "test_broken.py"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.INFRA_FAILED
    assert outcome.exit_code not in (0, 1)
    assert outcome.failure_kind == "pytest_internal_error"


def test_pytest_runner_kills_complete_process_group_on_timeout(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_sleep.py",
        """\
import subprocess
from pathlib import Path
import sys
import time

def test_sleep():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path("child.pid").write_text(str(child.pid), encoding="ascii")
    time.sleep(60)
""",
    )

    outcome = PytestRunner(poll_interval_seconds=0.02).run(
        _spec(tmp_path, "test_sleep.py", timeout_seconds=0.8), tmp_path, lambda: None
    )

    assert outcome.attempt_status is AttemptStatus.TIMED_OUT
    assert outcome.failure_kind == "timeout"
    child_pid = int((tmp_path / "child.pid").read_text(encoding="ascii"))
    _wait_until_stopped(child_pid)


@pytest.mark.parametrize("redirect_output", [False, True])
def test_pytest_runner_supervises_background_children_until_timeout(
    tmp_path: Path, redirect_output: bool
) -> None:
    output_arguments = ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" if redirect_output else ""
    _write(
        tmp_path,
        "test_background.py",
        f"""\
import subprocess
from pathlib import Path
import sys

def test_background():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(4)"]{output_arguments}
    )
    Path("background.pid").write_text(str(child.pid), encoding="ascii")
""",
    )

    started = time.monotonic()
    outcome = PytestRunner(poll_interval_seconds=0.02).run(
        _spec(tmp_path, "test_background.py", timeout_seconds=0.8),
        tmp_path,
        lambda: None,
    )
    elapsed = time.monotonic() - started
    child_pid = int((tmp_path / "background.pid").read_text(encoding="ascii"))
    try:
        assert outcome.attempt_status is AttemptStatus.TIMED_OUT
        assert elapsed < 3
        _wait_until_stopped(child_pid)
    finally:
        _kill_if_running(child_pid)


def test_pytest_runner_enforces_output_limit_after_direct_parent_exits(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "test_background_output.py",
        """\
import subprocess
from pathlib import Path
import sys

def test_background_output():
    child = subprocess.Popen([
        sys.executable,
        "-c",
        "import sys, time; time.sleep(0.5); print('x' * 200_000, flush=True); time.sleep(4)",
    ])
    Path("background.pid").write_text(str(child.pid), encoding="ascii")
""",
    )

    started = time.monotonic()
    outcome = PytestRunner(
        max_stream_bytes=1024,
        max_total_output_bytes=1536,
        poll_interval_seconds=0.01,
    ).run(_spec(tmp_path, "test_background_output.py"), tmp_path, lambda: None)
    elapsed = time.monotonic() - started
    child_pid = int((tmp_path / "background.pid").read_text(encoding="ascii"))
    try:
        assert outcome.attempt_status is AttemptStatus.INFRA_FAILED
        assert outcome.failure_kind == "output_limit_exceeded"
        assert elapsed < 3
        _wait_until_stopped(child_pid)
    finally:
        _kill_if_running(child_pid)


def test_pytest_runner_waits_for_short_background_child_before_passing(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "test_background.py",
        """\
import subprocess
from pathlib import Path
import sys

def test_background():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.3)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path("background.pid").write_text(str(child.pid), encoding="ascii")
""",
    )

    outcome = PytestRunner(poll_interval_seconds=0.02).run(
        _spec(tmp_path, "test_background.py", timeout_seconds=3), tmp_path, lambda: None
    )

    child_pid = int((tmp_path / "background.pid").read_text(encoding="ascii"))
    assert outcome.attempt_status is AttemptStatus.PASSED
    _wait_until_stopped(child_pid)


def test_heartbeat_failure_after_direct_parent_exit_kills_background_child(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "test_background.py",
        """\
import subprocess
from pathlib import Path
import sys

def test_background():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(4)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path("background.pid").write_text(str(child.pid), encoding="ascii")
""",
    )

    class HeartbeatFailed(RuntimeError):
        pass

    started = time.monotonic()

    def fail_after_parent_has_time_to_exit() -> None:
        if time.monotonic() - started >= 0.5:
            raise HeartbeatFailed("lease expired after pytest exited")

    with pytest.raises(HeartbeatFailed, match="lease expired"):
        PytestRunner(poll_interval_seconds=0.02).run(
            _spec(tmp_path, "test_background.py", timeout_seconds=3),
            tmp_path,
            fail_after_parent_has_time_to_exit,
        )

    child_pid = int((tmp_path / "background.pid").read_text(encoding="ascii"))
    try:
        _wait_until_stopped(child_pid)
    finally:
        _kill_if_running(child_pid)


def test_pytest_runner_calls_heartbeat_while_process_is_running(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_wait.py",
        "import time\ndef test_wait():\n    time.sleep(0.3)\n",
    )
    beats: list[float] = []

    outcome = PytestRunner(poll_interval_seconds=0.02).run(
        _spec(tmp_path, "test_wait.py"), tmp_path, lambda: beats.append(time.monotonic())
    )

    assert outcome.attempt_status is AttemptStatus.PASSED
    assert beats


def test_heartbeat_exception_kills_process_tree_and_is_propagated(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_wait.py",
        """\
import os
from pathlib import Path
import time

Path("pytest.pid").write_text(str(os.getpid()), encoding="ascii")

def test_wait():
    time.sleep(60)
""",
    )

    class HeartbeatFailed(RuntimeError):
        pass

    def fail_heartbeat() -> None:
        deadline = time.monotonic() + 3
        while not (tmp_path / "pytest.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise HeartbeatFailed("lease was lost")

    with pytest.raises(HeartbeatFailed, match="lease was lost"):
        PytestRunner(poll_interval_seconds=0.02).run(
            _spec(tmp_path, "test_wait.py", timeout_seconds=10), tmp_path, fail_heartbeat
        )

    pytest_pid = int((tmp_path / "pytest.pid").read_text(encoding="ascii"))
    _wait_until_stopped(pytest_pid)


def test_pytest_runner_stops_suite_when_output_limit_is_exceeded(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_output.py",
        "def test_output():\n    print('x' * 200_000)\n",
    )

    outcome = PytestRunner(
        max_stream_bytes=1024,
        max_total_output_bytes=1536,
        poll_interval_seconds=0.01,
    ).run(_spec(tmp_path, "test_output.py"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.INFRA_FAILED
    assert outcome.failure_kind == "output_limit_exceeded"
    assert _artifact_path(outcome, "stdout").stat().st_size <= 1024
    assert _artifact_path(outcome, "stderr").stat().st_size <= 1024
    assert (
        _artifact_path(outcome, "stdout").stat().st_size
        + _artifact_path(outcome, "stderr").stat().st_size
        <= 1536
    )


def test_pytest_runner_drains_stdout_and_stderr_concurrently(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_output.py",
        """\
import os

def test_output():
    os.write(1, b'o' * 100_000)
    os.write(2, b'e' * 100_000)
""",
    )

    outcome = PytestRunner(
        max_stream_bytes=256_000,
        max_total_output_bytes=512_000,
    ).run(_spec(tmp_path, "test_output.py"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.PASSED
    assert _artifact_path(outcome, "stdout").stat().st_size >= 100_000
    assert _artifact_path(outcome, "stderr").stat().st_size >= 100_000


def test_pytest_runner_does_not_inherit_secret_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUALITY_FLOW_TEST_SECRET", "do-not-leak")
    _write(
        tmp_path,
        "test_environment.py",
        """\
import os
from pathlib import Path
import sys

def test_clean_environment():
    assert "QUALITY_FLOW_TEST_SECRET" not in os.environ
    assert "PATH" not in os.environ
    assert os.environ["QUALITY_FLOW_PARAM_SCENARIO"] == "smoke"
    assert Path(sys.executable).is_absolute()
""",
    )

    outcome = PytestRunner().run(
        _spec(tmp_path, "test_environment.py", parameters={"scenario": "smoke"}),
        tmp_path,
        lambda: None,
    )

    assert outcome.attempt_status is AttemptStatus.PASSED
    assert "do-not-leak" not in _artifact_path(outcome, "stdout").read_text(
        encoding="utf-8"
    )
    assert "do-not-leak" not in _artifact_path(outcome, "stderr").read_text(
        encoding="utf-8"
    )


def test_pytest_runner_reports_missing_junit_as_infrastructure_failure(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "test_sample.py", "def test_ok():\n    assert True\n")
    _write(
        tmp_path,
        "conftest.py",
        """\
from pathlib import Path
import pytest

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    for report in Path.cwd().glob(".quality-flow-*\\junit.xml"):
        report.unlink(missing_ok=True)
    for report in Path.cwd().glob(".quality-flow-*/junit.xml"):
        report.unlink(missing_ok=True)
""",
    )

    outcome = PytestRunner().run(_spec(tmp_path, "test_sample.py"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.INFRA_FAILED
    assert outcome.failure_kind == "invalid_result"


def test_pytest_runner_maps_reparse_junit_to_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "test_sample.py", "def test_ok():\n    assert True\n")
    real_lstat = os.lstat

    def lstat_with_reparse_junit(path: object):
        if Path(path).name == "junit.xml":
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        return real_lstat(path)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess_module.os, "lstat", lstat_with_reparse_junit)

    outcome = PytestRunner().run(_spec(tmp_path, "test_sample.py"), tmp_path, lambda: None)

    assert outcome.attempt_status is AttemptStatus.INFRA_FAILED
    assert outcome.failure_kind == "invalid_result"


@pytest.mark.parametrize(
    "reserved_argument",
    [
        "--junitxml=outside.xml",
        "--capture=no",
        "--capture=sys",
        "-s",
    ],
)
def test_pytest_runner_rejects_reserved_result_and_capture_arguments(
    tmp_path: Path, reserved_argument: str
) -> None:
    _write(tmp_path, "test_sample.py", "def test_ok():\n    assert True\n")
    spec = _spec(tmp_path, "test_sample.py", reserved_argument)

    with pytest.raises(RunnerConfigurationError, match="reserved"):
        PytestRunner().run(spec, tmp_path, lambda: None)

    assert not (tmp_path.parent / "outside.xml").exists()


@pytest.mark.parametrize("target", ["../outside.py", "test_ok.py;echo hacked"])
def test_pytest_runner_rejects_unsafe_test_target(tmp_path: Path, target: str) -> None:
    with pytest.raises(RunnerConfigurationError):
        PytestRunner().run(_spec(tmp_path, target), tmp_path, lambda: None)


def test_pytest_runner_rejects_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(RunnerConfigurationError, match="workspace"):
        PytestRunner().run(
            _spec(tmp_path, "test_sample.py"), tmp_path / "missing", lambda: None
        )


def test_pytest_runner_rejects_workspace_outside_allowed_root(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_workspace = tmp_path / "outside"
    outside_workspace.mkdir()
    _write(outside_workspace, "test_sample.py", "def test_ok():\n    assert True\n")
    spec = ExecutionSpec(
        argv=("python", "-m", "pytest", "test_sample.py"),
        timeout_seconds=10,
        parameters={},
        gate_policy=GatePolicy(),
        allowed_workspace_root=allowed_root,
    )

    with pytest.raises(RunnerConfigurationError, match="allowed workspace root"):
        PytestRunner().run(spec, outside_workspace, lambda: None)


def test_pytest_runner_rejects_symlink_workspace_when_supported(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real"
    real_workspace.mkdir()
    linked_workspace = tmp_path / "linked"
    try:
        linked_workspace.symlink_to(real_workspace, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symbolic links unavailable on this host: {error}")

    with pytest.raises(RunnerConfigurationError, match="link|reparse"):
        PytestRunner().run(
            _spec(tmp_path, "test_sample.py"), linked_workspace, lambda: None
        )
