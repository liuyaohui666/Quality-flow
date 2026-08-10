from __future__ import annotations

import os
from pathlib import Path
import signal
import stat
import sys
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

from quality_flow.runners import subprocess_runner as subprocess_module
from quality_flow.runners.subprocess_runner import (
    RunnerConfigurationError,
    SafeSubprocessExecutor,
    UnsafeRunnerResult,
    build_clean_environment,
    prepare_staging_directory,
    validate_result_file,
    validate_workspace,
)


def test_validate_result_file_reports_reparse_point_as_unsafe_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = tmp_path / "result.xml"
    result.write_text("<testsuite />", encoding="utf-8")
    real_lstat = os.lstat

    def lstat_with_reparse_point(path: object):
        if Path(path) == result:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        return real_lstat(path)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess_module.os, "lstat", lstat_with_reparse_point)

    staging = prepare_staging_directory(tmp_path)

    with pytest.raises(UnsafeRunnerResult, match="link|reparse"):
        validate_result_file(result, tmp_path, staging)


def test_validate_result_file_rejects_hardlink_to_external_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.xml"
    outside.write_text("outside", encoding="utf-8")
    linked_result = workspace / "result.xml"
    os.link(outside, linked_result)

    staging = prepare_staging_directory(workspace)

    with pytest.raises(UnsafeRunnerResult, match="hard link"):
        validate_result_file(linked_result, workspace, staging)


def test_validate_result_file_returns_private_snapshot_not_mutable_source(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "result.xml"
    source.write_text("original", encoding="utf-8")
    staging = prepare_staging_directory(workspace)

    snapshot = validate_result_file(source, workspace, staging)
    source.write_text("replaced", encoding="utf-8")

    assert snapshot != source
    assert snapshot.read_text(encoding="utf-8") == "original"
    assert snapshot.is_relative_to(staging)
    assert not snapshot.is_relative_to(workspace)


def test_validate_result_file_rejects_result_larger_than_snapshot_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "result.xml"
    source.write_bytes(b"x" * 17)
    staging = prepare_staging_directory(workspace)

    with pytest.raises(UnsafeRunnerResult, match="size limit"):
        validate_result_file(source, workspace, staging, max_bytes=16)

    assert not list(staging.iterdir())


def test_prepare_staging_directory_rejects_suite_workspace_overlap(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RunnerConfigurationError, match="staging"):
        prepare_staging_directory(workspace, staging_parent=workspace / "owned")


def test_validate_workspace_rejects_reparse_allowed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_lstat = os.lstat

    def lstat_with_reparse_root(path: object):
        if Path(path) == tmp_path:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return real_lstat(path)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess_module.os, "lstat", lstat_with_reparse_root)

    with pytest.raises(RunnerConfigurationError, match="link|reparse"):
        validate_workspace(workspace, tmp_path)


def test_posix_tree_cleanup_escalates_after_direct_parent_has_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        pid = 2718

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float) -> int:
            return 0

    signals: list[int] = []

    def missing_root(pid: int):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(subprocess_module.os, "name", "posix")
    monkeypatch.setattr(subprocess_module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(subprocess_module.psutil, "Process", missing_root)
    monkeypatch.setattr(
        subprocess_module.os,
        "killpg",
        lambda _pid, sent_signal: signals.append(sent_signal),
        raising=False,
    )
    monkeypatch.setattr(
        subprocess_module,
        "_posix_process_group_is_running",
        lambda _pid: True,
    )
    monkeypatch.setattr(
        subprocess_module,
        "_wait_for_posix_process_group_exit",
        lambda _pid, _timeout: False,
    )

    subprocess_module._terminate_process_tree(ExitedProcess())  # type: ignore[arg-type]

    assert signals == [signal.SIGTERM, 9]


def test_blocked_heartbeat_cannot_extend_hard_process_timeout(
    tmp_path: Path,
) -> None:
    result_directory = tmp_path / "results"
    result_directory.mkdir()

    execution = SafeSubprocessExecutor(poll_interval_seconds=0.02).execute(
        [sys.executable, "-c", "import time; time.sleep(4)"],
        workspace=tmp_path,
        allowed_workspace_root=tmp_path,
        timeout_seconds=0.3,
        heartbeat=lambda: time.sleep(2),
        result_directory=result_directory,
        environment=build_clean_environment({}),
    )
    elapsed = (execution.finished_at - execution.started_at).total_seconds()

    assert execution.timed_out is True
    assert elapsed < 1.5


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_posix_timeout_reaps_a_spawned_child_process(tmp_path: Path) -> None:
    root = tmp_path / "root.py"
    root.write_text(
        """\
import subprocess
from pathlib import Path
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path("child.pid").write_text(str(child.pid), encoding="ascii")
time.sleep(30)
""",
        encoding="utf-8",
    )
    result_directory = tmp_path / "results"
    result_directory.mkdir()

    execution = SafeSubprocessExecutor(poll_interval_seconds=0.02).execute(
        [sys.executable, str(root)],
        workspace=tmp_path,
        allowed_workspace_root=tmp_path,
        timeout_seconds=0.5,
        heartbeat=lambda: None,
        result_directory=result_directory,
        environment=build_clean_environment({}),
    )
    child_pid = int((tmp_path / "child.pid").read_text(encoding="ascii"))
    try:
        assert execution.timed_out is True
        assert not psutil.pid_exists(child_pid)
    finally:
        try:
            psutil.Process(child_pid).kill()
        except psutil.Error:
            pass


def test_heartbeat_supervision_uses_a_bounded_daemon_worker_set(
    tmp_path: Path,
) -> None:
    result_directory = tmp_path / "results"
    result_directory.mkdir()
    release = threading.Event()
    before = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("quality-flow-heartbeat-")
    }
    try:
        SafeSubprocessExecutor(poll_interval_seconds=0.02).execute(
            [sys.executable, "-c", "import time; time.sleep(4)"],
            workspace=tmp_path,
            allowed_workspace_root=tmp_path,
            timeout_seconds=0.15,
            heartbeat=release.wait,
            result_directory=result_directory,
            environment=build_clean_environment({}),
        )
        after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("quality-flow-heartbeat-")
        }
        assert after
        assert len(after) <= 4
        assert before <= after
        assert all(
            thread.daemon
            for thread in threading.enumerate()
            if thread.name.startswith("quality-flow-heartbeat-")
        )
    finally:
        release.set()


def test_quick_heartbeat_exception_is_propagated_by_identity(
    tmp_path: Path,
) -> None:
    result_directory = tmp_path / "results"
    result_directory.mkdir()
    failure = RuntimeError("lease update failed")

    def fail() -> None:
        raise failure

    with pytest.raises(RuntimeError) as raised:
        SafeSubprocessExecutor(poll_interval_seconds=0.02).execute(
            [sys.executable, "-c", "import time; time.sleep(4)"],
            workspace=tmp_path,
            allowed_workspace_root=tmp_path,
            timeout_seconds=2,
            heartbeat=fail,
            result_directory=result_directory,
            environment=build_clean_environment({}),
        )

    assert raised.value is failure


def test_heartbeat_exception_completed_during_normal_exit_cleanup_is_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_directory = tmp_path / "results"
    result_directory.mkdir()
    callback_started = threading.Event()
    release_callback = threading.Event()
    failure = RuntimeError("late lease update failed")

    def fail_during_cleanup() -> None:
        callback_started.set()
        release_callback.wait()
        raise failure

    real_cancel = subprocess_module._HeartbeatCall.cancel

    def complete_callback_then_cancel(call):
        assert callback_started.wait(timeout=1)
        release_callback.set()
        assert call.done.wait(timeout=1)
        return real_cancel(call)

    monkeypatch.setattr(
        subprocess_module._HeartbeatCall,
        "cancel",
        complete_callback_then_cancel,
    )

    with pytest.raises(RuntimeError) as raised:
        SafeSubprocessExecutor(poll_interval_seconds=0.02).execute(
            [sys.executable, "-c", "import time; time.sleep(0.1)"],
            workspace=tmp_path,
            allowed_workspace_root=tmp_path,
            timeout_seconds=2,
            heartbeat=fail_during_cleanup,
            result_directory=result_directory,
            environment=build_clean_environment({}),
        )

    assert raised.value is failure


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_windows_job_assignment_failure_kills_suspended_process_and_closes_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_directory = tmp_path / "results"
    result_directory.mkdir()
    observed_pid: list[int] = []
    close_calls: list[object] = []
    job_type = subprocess_module._WindowsJob
    real_close = job_type.close

    def fail_assignment(self, process) -> None:
        observed_pid.append(process.pid)
        raise OSError("AssignProcessToJobObject failed")

    def record_close(self) -> None:
        close_calls.append(self)
        real_close(self)

    monkeypatch.setattr(job_type, "assign_and_resume", fail_assignment)
    monkeypatch.setattr(job_type, "close", record_close)

    execution = SafeSubprocessExecutor().execute(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        workspace=tmp_path,
        allowed_workspace_root=tmp_path,
        timeout_seconds=2,
        heartbeat=lambda: None,
        result_directory=result_directory,
        environment=build_clean_environment({}),
    )

    assert execution.infrastructure_error == "process_supervision_failed"
    assert len(observed_pid) == 1
    assert not psutil.pid_exists(observed_pid[0])
    assert len(close_calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_windows_job_handle_is_closed_once_after_successful_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_directory = tmp_path / "results"
    result_directory.mkdir()
    close_calls: list[object] = []
    job_type = subprocess_module._WindowsJob
    real_close = job_type.close

    def record_close(self) -> None:
        close_calls.append(self)
        real_close(self)

    monkeypatch.setattr(job_type, "close", record_close)

    execution = SafeSubprocessExecutor().execute(
        [sys.executable, "-c", "pass"],
        workspace=tmp_path,
        allowed_workspace_root=tmp_path,
        timeout_seconds=2,
        heartbeat=lambda: None,
        result_directory=result_directory,
        environment=build_clean_environment({}),
    )

    assert execution.exit_code == 0
    assert execution.infrastructure_error is None
    assert len(close_calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_output_limit_reaps_grandchild_after_both_parents_exit(
    tmp_path: Path,
) -> None:
    middle = tmp_path / "middle_output.py"
    middle.write_text(
        """\
import subprocess
from pathlib import Path
import sys

code = "import sys,time;sys.stdout.write('x'*200000);sys.stdout.flush();time.sleep(4)"
child = subprocess.Popen([sys.executable, "-c", code])
Path("output-grandchild.pid").write_text(str(child.pid), encoding="ascii")
""",
        encoding="utf-8",
    )
    root = tmp_path / "root_output.py"
    root.write_text(
        """\
import subprocess
import sys

subprocess.run([sys.executable, "middle_output.py"], check=True)
""",
        encoding="utf-8",
    )
    result_directory = tmp_path / "results"
    result_directory.mkdir()

    execution = SafeSubprocessExecutor(
        poll_interval_seconds=0.02,
        max_stream_bytes=1024,
        max_total_output_bytes=1536,
    ).execute(
        [sys.executable, str(root)],
        workspace=tmp_path,
        allowed_workspace_root=tmp_path,
        timeout_seconds=3,
        heartbeat=lambda: None,
        result_directory=result_directory,
        environment=build_clean_environment({}),
    )
    child_pid = int(
        (tmp_path / "output-grandchild.pid").read_text(encoding="ascii")
    )
    try:
        assert execution.output_limit_exceeded is True
        assert not psutil.pid_exists(child_pid)
    finally:
        try:
            psutil.Process(child_pid).kill()
        except psutil.Error:
            pass


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_heartbeat_failure_reaps_grandchild_after_both_parents_exit(
    tmp_path: Path,
) -> None:
    middle = tmp_path / "middle_heartbeat.py"
    middle.write_text(
        """\
import subprocess
from pathlib import Path
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(4)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path("heartbeat-grandchild.pid").write_text(str(child.pid), encoding="ascii")
""",
        encoding="utf-8",
    )
    root = tmp_path / "root_heartbeat.py"
    root.write_text(
        """\
import subprocess
import sys

subprocess.run(
    [sys.executable, "middle_heartbeat.py"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=True,
)
""",
        encoding="utf-8",
    )
    result_directory = tmp_path / "results"
    result_directory.mkdir()
    failure = RuntimeError("lease lost")

    def fail_after_pid_exists() -> None:
        deadline = time.monotonic() + 2
        while not (tmp_path / "heartbeat-grandchild.pid").exists():
            if time.monotonic() >= deadline:
                raise AssertionError("grandchild did not start")
            time.sleep(0.01)
        raise failure

    with pytest.raises(RuntimeError) as raised:
        SafeSubprocessExecutor(poll_interval_seconds=0.02).execute(
            [sys.executable, str(root)],
            workspace=tmp_path,
            allowed_workspace_root=tmp_path,
            timeout_seconds=3,
            heartbeat=fail_after_pid_exists,
            result_directory=result_directory,
            environment=build_clean_environment({}),
        )

    child_pid = int(
        (tmp_path / "heartbeat-grandchild.pid").read_text(encoding="ascii")
    )
    try:
        assert raised.value is failure
        assert not psutil.pid_exists(child_pid)
    finally:
        try:
            psutil.Process(child_pid).kill()
        except psutil.Error:
            pass


@pytest.mark.parametrize("_attempt", range(3))
def test_executor_tracks_child_when_direct_parent_exits_between_polls(
    tmp_path: Path, _attempt: int
) -> None:
    script = tmp_path / "spawn_and_exit.py"
    script.write_text(
        """\
import subprocess
from pathlib import Path
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(4)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path("orphan.pid").write_text(str(child.pid), encoding="ascii")
""",
        encoding="utf-8",
    )
    result_directory = tmp_path / "results"
    result_directory.mkdir()
    started = time.monotonic()

    execution = SafeSubprocessExecutor(poll_interval_seconds=0.2).execute(
        [sys.executable, str(script)],
        workspace=tmp_path,
        allowed_workspace_root=tmp_path,
        timeout_seconds=0.8,
        heartbeat=lambda: None,
        result_directory=result_directory,
        environment=build_clean_environment({}),
    )
    elapsed = time.monotonic() - started
    child_pid = int((tmp_path / "orphan.pid").read_text(encoding="ascii"))
    try:
        assert execution.timed_out is True
        assert elapsed < 4
        assert not psutil.pid_exists(child_pid)
    finally:
        try:
            psutil.Process(child_pid).kill()
        except psutil.Error:
            pass


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
@pytest.mark.parametrize("_attempt", range(3))
def test_executor_tracks_two_generations_that_exit_between_polls(
    tmp_path: Path, _attempt: int
) -> None:
    middle = tmp_path / "middle.py"
    middle.write_text(
        """\
import subprocess
from pathlib import Path
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(4)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path("grandchild.pid").write_text(str(child.pid), encoding="ascii")
""",
        encoding="utf-8",
    )
    root = tmp_path / "root.py"
    root.write_text(
        """\
import subprocess
import sys

subprocess.run(
    [sys.executable, "middle.py"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=True,
)
""",
        encoding="utf-8",
    )
    result_directory = tmp_path / "results"
    result_directory.mkdir()

    execution = SafeSubprocessExecutor(poll_interval_seconds=0.2).execute(
        [sys.executable, str(root)],
        workspace=tmp_path,
        allowed_workspace_root=tmp_path,
        timeout_seconds=0.8,
        heartbeat=lambda: None,
        result_directory=result_directory,
        environment=build_clean_environment({}),
    )
    child_pid = int((tmp_path / "grandchild.pid").read_text(encoding="ascii"))
    try:
        assert execution.timed_out is True
        assert not psutil.pid_exists(child_pid)
    finally:
        try:
            psutil.Process(child_pid).kill()
        except psutil.Error:
            pass
