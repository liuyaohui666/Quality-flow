"""Shared, bounded subprocess execution for trusted registered suites."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import math
import os
from pathlib import Path
import queue
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import BinaryIO
from uuid import uuid4

import psutil

if os.name == "nt":  # pragma: no cover - imported and exercised on Windows
    import win32api
    import win32con
    import win32job
    import win32process
else:  # pragma: no cover - keeps Linux images independent from pywin32
    win32api = None
    win32con = None
    win32job = None
    win32process = None


class RunnerConfigurationError(ValueError):
    """A runner specification or workspace crosses the registered boundary."""


class UnsafeRunnerResult(RuntimeError):
    """A required result path is absent, linked, or outside its workspace."""


@dataclass(frozen=True)
class ProcessExecution:
    exit_code: int | None
    started_at: datetime
    finished_at: datetime
    stdout_path: Path
    stderr_path: Path
    timed_out: bool = False
    output_limit_exceeded: bool = False
    infrastructure_error: str | None = None


_SAFE_PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SAFE_PARAMETER_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_ALLOWED_INJECTED_ENVIRONMENT = frozenset({"QUALITY_FLOW_TARGET_URL"})
_DEFAULT_STAGING_PARENT = Path(tempfile.gettempdir()) / "quality-flow-runner-staging"


def validate_workspace(workspace: Path, allowed_workspace_root: Path) -> Path:
    candidate = Path(workspace)
    root_candidate = Path(allowed_workspace_root)
    if any(part == ".." for part in candidate.parts):
        raise RunnerConfigurationError("workspace must not contain parent traversal")
    if any(part == ".." for part in root_candidate.parts):
        raise RunnerConfigurationError(
            "allowed workspace root must not contain parent traversal"
        )
    try:
        _ensure_no_links(root_candidate)
        resolved_root = root_candidate.resolve(strict=True)
        _ensure_no_links(candidate)
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RunnerConfigurationError("workspace is unavailable") from error
    if not resolved_root.is_dir():
        raise RunnerConfigurationError("allowed workspace root must be a directory")
    if not resolved.is_dir():
        raise RunnerConfigurationError("workspace must be a directory")
    _assert_inside(
        resolved,
        resolved_root,
        RunnerConfigurationError,
        message="workspace is outside the allowed workspace root",
    )
    return resolved


def prepare_result_directory(
    workspace: Path, allowed_workspace_root: Path
) -> Path:
    resolved_workspace = validate_workspace(workspace, allowed_workspace_root)
    result_directory = Path(
        tempfile.mkdtemp(prefix=".quality-flow-", dir=resolved_workspace)
    )
    _ensure_no_links(result_directory)
    _assert_inside(result_directory.resolve(strict=True), resolved_workspace)
    return result_directory


def prepare_staging_directory(
    workspace: Path,
    *,
    staging_parent: Path | None = None,
) -> Path:
    """Create a private result snapshot directory outside suite control."""
    resolved_workspace = validate_workspace(workspace, workspace)
    parent_candidate = Path(staging_parent or _DEFAULT_STAGING_PARENT)
    if any(part == ".." for part in parent_candidate.parts):
        raise RunnerConfigurationError("staging parent must not contain traversal")
    try:
        parent_candidate.mkdir(parents=True, exist_ok=True)
        _ensure_no_links(parent_candidate)
        resolved_parent = parent_candidate.resolve(strict=True)
    except OSError as error:
        raise RunnerConfigurationError("staging parent is unavailable") from error
    if not resolved_parent.is_dir():
        raise RunnerConfigurationError("staging parent must be a directory")
    _assert_disjoint_storage(resolved_parent, resolved_workspace)
    try:
        staging = Path(
            tempfile.mkdtemp(prefix="execution-", dir=resolved_parent)
        ).resolve(strict=True)
        os.chmod(staging, 0o700)
        _ensure_no_links(staging)
    except OSError as error:
        raise RunnerConfigurationError("staging directory could not be created") from error
    _assert_inside(staging, resolved_parent)
    _assert_disjoint_storage(staging, resolved_workspace)
    return staging


def validate_suite_path(
    raw_path: str,
    workspace: Path,
    *,
    regular_file: bool = False,
) -> Path:
    if not raw_path or any(character in raw_path for character in ";|&`\r\n\0"):
        raise RunnerConfigurationError("suite path contains unsafe characters")
    candidate_path = Path(raw_path)
    if candidate_path.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate_path.parts
    ):
        raise RunnerConfigurationError("suite path must stay inside workspace")
    candidate = workspace / candidate_path
    try:
        _ensure_no_links(candidate)
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RunnerConfigurationError("suite path is unavailable") from error
    _assert_inside(resolved, workspace)
    if regular_file and not resolved.is_file():
        raise RunnerConfigurationError("suite path must be a regular file")
    if not regular_file and not (resolved.is_file() or resolved.is_dir()):
        raise RunnerConfigurationError("suite path must be a file or directory")
    return resolved


def validate_result_file(
    path: Path,
    workspace: Path,
    staging_directory: Path,
    *,
    max_bytes: int = 50 * 1024 * 1024,
) -> Path:
    """Copy one verified result into a service-owned execution staging root."""
    if max_bytes <= 0:
        raise ValueError("runner result size limit must be positive")
    resolved_workspace = validate_workspace(workspace, workspace)
    candidate = Path(path)
    try:
        _ensure_no_links(candidate, UnsafeRunnerResult)
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise UnsafeRunnerResult("required runner result is missing") from error
    _assert_inside(resolved, resolved_workspace, UnsafeRunnerResult)

    try:
        _ensure_no_links(staging_directory, UnsafeRunnerResult)
        staging = Path(staging_directory).resolve(strict=True)
    except OSError as error:
        raise UnsafeRunnerResult("runner staging directory is unavailable") from error
    if not staging.is_dir():
        raise UnsafeRunnerResult("runner staging path must be a directory")
    _assert_disjoint_storage(
        staging,
        resolved_workspace,
        error_type=UnsafeRunnerResult,
    )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as error:
        raise UnsafeRunnerResult("required runner result is unavailable") from error

    snapshot_path: Path | None = None
    final_path: Path | None = None
    try:
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode):
            raise UnsafeRunnerResult(
                "required runner result must be a regular file"
            )
        if opened_status.st_nlink != 1:
            raise UnsafeRunnerResult(
                "required runner result must not be a hard link"
            )
        if opened_status.st_size > max_bytes:
            raise UnsafeRunnerResult("runner result exceeds the size limit")

        _ensure_no_links(resolved, UnsafeRunnerResult)
        current_status = os.stat(resolved)
        if not os.path.samestat(opened_status, current_status):
            raise UnsafeRunnerResult(
                "required runner result changed during validation"
            )

        with os.fdopen(descriptor, "rb") as source_file:
            descriptor = -1
            with tempfile.NamedTemporaryFile(
                mode="xb",
                dir=staging,
                prefix=".partial-result-",
                delete=False,
            ) as snapshot_file:
                snapshot_path = Path(snapshot_file.name)
                copied_bytes = 0
                while chunk := source_file.read(1024 * 1024):
                    copied_bytes += len(chunk)
                    if copied_bytes > max_bytes:
                        raise UnsafeRunnerResult(
                            "runner result exceeds the size limit"
                        )
                    snapshot_file.write(chunk)
                snapshot_file.flush()
                os.fsync(snapshot_file.fileno())

        _ensure_no_links(snapshot_path, UnsafeRunnerResult)
        partial_snapshot = snapshot_path.resolve(strict=True)
        _assert_inside(partial_snapshot, staging, UnsafeRunnerResult)
        final_path = staging / uuid4().hex
        partial_snapshot.replace(final_path)
        snapshot_path = None
        _ensure_no_links(final_path, UnsafeRunnerResult)
        snapshot = final_path.resolve(strict=True)
        _assert_inside(snapshot, staging, UnsafeRunnerResult)
        snapshot_status = snapshot.stat()
        if not stat.S_ISREG(snapshot_status.st_mode) or snapshot_status.st_nlink != 1:
            raise UnsafeRunnerResult("runner result snapshot is not a private file")
        return snapshot
    except Exception:
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def build_clean_environment(
    parameters: Mapping[str, str],
    injected_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    if os.name == "nt":
        for name in ("SystemRoot", "WINDIR", "COMSPEC"):
            value = os.environ.get(name)
            if value:
                environment[name] = value

    supplied = dict(injected_environment or {})
    unknown_environment = set(supplied) - _ALLOWED_INJECTED_ENVIRONMENT
    if unknown_environment:
        raise RunnerConfigurationError("runner environment contains a non-allowlisted key")
    for name in _ALLOWED_INJECTED_ENVIRONMENT:
        value = supplied.get(name, os.environ.get(name))
        if value is not None:
            if "\0" in value or "\r" in value or "\n" in value:
                raise RunnerConfigurationError("runner environment contains unsafe data")
            environment[name] = value

    for name, value in parameters.items():
        if not _SAFE_PARAMETER_NAME.fullmatch(name) or not _SAFE_PARAMETER_VALUE.fullmatch(
            value
        ):
            raise RunnerConfigurationError("runner parameter is not safe for environment use")
        environment[f"QUALITY_FLOW_PARAM_{name.upper()}"] = value
    return environment


class _HeartbeatCall:
    """One cancellable heartbeat invocation owned by the bounded dispatcher."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.error: BaseException | None = None
        self._started = False
        self._lock = threading.Lock()

    def run(self) -> None:
        with self._lock:
            if self.cancelled.is_set():
                self.done.set()
                return
            self._started = True
        error: BaseException | None = None
        try:
            self.callback()
        except BaseException as callback_error:
            error = callback_error
        finally:
            with self._lock:
                self.error = error
                self.done.set()

    def cancel(self) -> BaseException | None:
        """Atomically cancel a queued call and return any completed error."""
        with self._lock:
            self.cancelled.set()
            if not self._started:
                self.done.set()
            return self.error


class _HeartbeatDispatcher:
    """Process-wide bounded daemon pool so blocked callbacks cannot grow threads."""

    def __init__(self, *, worker_count: int = 4, queue_capacity: int = 32) -> None:
        self._worker_count = worker_count
        self._queue: queue.Queue[_HeartbeatCall] = queue.Queue(
            maxsize=queue_capacity
        )
        self._start_lock = threading.Lock()
        self._started = False

    def submit(self, callback: Callable[[], None]) -> _HeartbeatCall:
        self._ensure_started()
        call = _HeartbeatCall(callback)
        try:
            self._queue.put_nowait(call)
        except queue.Full:
            call.error = RuntimeError("heartbeat dispatcher capacity exhausted")
            call.done.set()
        return call

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            for index in range(self._worker_count):
                threading.Thread(
                    target=self._worker,
                    name=f"quality-flow-heartbeat-{index}",
                    daemon=True,
                ).start()
            self._started = True

    def _worker(self) -> None:
        while True:
            self._queue.get().run()


_HEARTBEAT_DISPATCHER = _HeartbeatDispatcher()


class _WindowsJob:
    """A kill-on-close Job Object used to contain every Windows descendant."""

    def __init__(self) -> None:
        if os.name != "nt" or win32job is None:
            raise OSError("Windows Job Objects are unavailable")
        self._handle = win32job.CreateJobObject(None, "")
        self._closed = False
        try:
            information = win32job.QueryInformationJobObject(
                self._handle,
                win32job.JobObjectExtendedLimitInformation,
            )
            information["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self._handle,
                win32job.JobObjectExtendedLimitInformation,
                information,
            )
        except Exception:
            self.close()
            raise

    @property
    def creation_flags(self) -> int:
        assert win32process is not None
        return win32process.CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        assert win32job is not None
        native_handle = getattr(process, "_handle", None)
        if native_handle is None:
            raise OSError("subprocess native handle is unavailable")
        win32job.AssignProcessToJobObject(self._handle, native_handle)
        threads = psutil.Process(process.pid).threads()
        if len(threads) != 1:
            raise OSError("suspended process did not expose one primary thread")
        assert win32api is not None
        assert win32con is not None
        assert win32process is not None
        thread_handle = win32api.OpenThread(
            win32con.THREAD_SUSPEND_RESUME,
            False,
            threads[0].id,
        )
        try:
            previous_suspend_count = win32process.ResumeThread(thread_handle)
        finally:
            win32api.CloseHandle(thread_handle)
        if previous_suspend_count != 1:
            raise OSError(
                "primary thread was not resumed from exactly one suspension"
            )

    def active_processes(self) -> int:
        assert win32job is not None
        information = win32job.QueryInformationJobObject(
            self._handle,
            win32job.JobObjectBasicAccountingInformation,
        )
        return int(information["ActiveProcesses"])

    def process_ids(self) -> tuple[int, ...]:
        assert win32job is not None
        return tuple(
            int(process_id)
            for process_id in win32job.QueryInformationJobObject(
                self._handle,
                win32job.JobObjectBasicProcessIdList,
            )
        )

    def terminate(self) -> None:
        if self._closed:
            return
        assert win32job is not None
        processes: list[psutil.Process] = []
        for process_id in self.process_ids():
            try:
                processes.append(psutil.Process(process_id))
            except psutil.Error:
                pass
        win32job.TerminateJobObject(self._handle, 1)
        psutil.wait_procs(processes, timeout=2)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if win32api is not None:
            win32api.CloseHandle(self._handle)


class _ProcessSupervisionError(OSError):
    pass


class SafeSubprocessExecutor:
    """Execute one argument vector while draining bounded output concurrently."""

    def __init__(
        self,
        *,
        poll_interval_seconds: float = 0.05,
        max_stream_bytes: int = 1024 * 1024,
        max_total_output_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        if (
            not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be positive")
        if max_stream_bytes <= 0 or max_total_output_bytes <= 0:
            raise ValueError("output limits must be positive")
        self._poll_interval_seconds = poll_interval_seconds
        self._max_stream_bytes = max_stream_bytes
        self._max_total_output_bytes = max_total_output_bytes

    def execute(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
        result_directory: Path,
        allowed_workspace_root: Path,
        environment: Mapping[str, str],
    ) -> ProcessExecution:
        resolved_workspace = validate_workspace(workspace, allowed_workspace_root)
        _assert_inside(result_directory.resolve(strict=True), resolved_workspace)
        stdout_path = result_directory / "stdout.log"
        stderr_path = result_directory / "stderr.log"
        started_at = datetime.now(UTC)
        try:
            process, windows_job = _launch_supervised_process(
                argv,
                workspace=resolved_workspace,
                environment=environment,
            )
        except _ProcessSupervisionError:
            stdout_path.touch()
            stderr_path.touch()
            return ProcessExecution(
                exit_code=None,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                infrastructure_error="process_supervision_failed",
            )
        except OSError:
            stdout_path.touch()
            stderr_path.touch()
            return ProcessExecution(
                exit_code=None,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                infrastructure_error="process_launch_failed",
            )

        assert process.stdout is not None
        assert process.stderr is not None
        budget = _OutputBudget(
            per_stream_limit=self._max_stream_bytes,
            total_limit=self._max_total_output_bytes,
        )
        reader_errors: list[BaseException] = []
        threads = [
            threading.Thread(
                target=_drain_stream,
                args=(process.stdout, stdout_path, "stdout", budget, reader_errors),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_stream,
                args=(process.stderr, stderr_path, "stderr", budget, reader_errors),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        if os.name == "posix":
            try:
                root_process: psutil.Process | None = psutil.Process(process.pid)
                root_started_at = root_process.create_time()
            except psutil.Error:
                root_process = None
                root_started_at = started_at.timestamp()
        else:
            root_process = None
            root_started_at = started_at.timestamp()
        tracked_descendants: dict[int, psutil.Process] = {}
        timed_out = False
        heartbeat_error: BaseException | None = None
        supervision_error: BaseException | None = None
        heartbeat_call = _HEARTBEAT_DISPATCHER.submit(heartbeat)
        deadline = time.monotonic() + timeout_seconds
        idle_observations = 0
        while True:
            if heartbeat_call is not None and heartbeat_call.done.is_set():
                if heartbeat_call.error is not None:
                    heartbeat_error = heartbeat_call.error
                    _stop_execution(process, windows_job, tracked_descendants.values())
                    break
                heartbeat_call = None

            windows_tree_is_running = False
            if windows_job is not None:
                try:
                    windows_tree_is_running = windows_job.active_processes() > 0
                except BaseException as error:
                    supervision_error = error
                    _stop_execution(process, windows_job, tracked_descendants.values())
                    break
            else:
                _remember_descendants(
                    root_process,
                    process.pid,
                    root_started_at,
                    tracked_descendants,
                )
            parent_is_running = process.poll() is None
            descendants_are_running = _discard_stopped_processes(
                tracked_descendants
            )
            process_group_is_running = _posix_process_group_is_running(
                process.pid
            )
            readers_are_running = any(thread.is_alive() for thread in threads)
            has_live_execution_resource = (
                parent_is_running
                or descendants_are_running
                or process_group_is_running
                or windows_tree_is_running
                or readers_are_running
            )
            if has_live_execution_resource:
                idle_observations = 0
            else:
                idle_observations += 1
                if idle_observations >= 2:
                    break
                time.sleep(min(self._poll_interval_seconds, 0.01))
                continue
            if budget.exceeded.is_set():
                _stop_execution(process, windows_job, tracked_descendants.values())
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _stop_execution(process, windows_job, tracked_descendants.values())
                break
            if heartbeat_call is None:
                heartbeat_call = _HEARTBEAT_DISPATCHER.submit(heartbeat)
            time.sleep(self._poll_interval_seconds)

        if heartbeat_call is not None:
            cleanup_heartbeat_error = heartbeat_call.cancel()
            if (
                heartbeat_error is None
                and cleanup_heartbeat_error is not None
                and not timed_out
                and not budget.exceeded.is_set()
                and supervision_error is None
            ):
                heartbeat_error = cleanup_heartbeat_error
        try:
            _wait_for_process(process)
            for thread in threads:
                thread.join(timeout=2)
            for thread, pipe in zip(
                threads, (process.stdout, process.stderr), strict=True
            ):
                if thread.is_alive():
                    reader_errors.append(
                        RuntimeError("output reader did not stop after process cleanup")
                    )
                    continue
                try:
                    pipe.close()
                except OSError:
                    pass
        finally:
            if windows_job is not None:
                windows_job.close()

        if heartbeat_error is not None:
            raise heartbeat_error

        return ProcessExecution(
            exit_code=process.returncode,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timed_out=timed_out,
            output_limit_exceeded=budget.exceeded.is_set(),
            infrastructure_error=(
                "process_supervision_failed"
                if supervision_error is not None
                else "output_capture_failed"
                if reader_errors
                else None
            ),
        )


def _launch_supervised_process(
    argv: Sequence[str],
    *,
    workspace: Path,
    environment: Mapping[str, str],
) -> tuple[subprocess.Popen[bytes], _WindowsJob | None]:
    windows_job: _WindowsJob | None = None
    process: subprocess.Popen[bytes] | None = None
    if os.name == "nt":
        try:
            windows_job = _WindowsJob()
        except Exception as error:
            raise _ProcessSupervisionError("Job Object creation failed") from error
    try:
        process = subprocess.Popen(
            list(argv),
            shell=False,
            cwd=workspace,
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
            creationflags=(windows_job.creation_flags if windows_job else 0),
        )
    except OSError:
        if windows_job is not None:
            windows_job.close()
        raise

    if windows_job is None:
        return process, None

    try:
        windows_job.assign_and_resume(process)
    except Exception as error:
        try:
            windows_job.terminate()
        except Exception:
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        _wait_for_process(process)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        windows_job.close()
        raise _ProcessSupervisionError(
            "suspended process could not be assigned and resumed"
        ) from error
    return process, windows_job


def _stop_execution(
    process: subprocess.Popen[bytes],
    windows_job: _WindowsJob | None,
    known_descendants: Sequence[psutil.Process],
) -> None:
    if windows_job is not None:
        try:
            windows_job.terminate()
            _wait_for_process(process)
            return
        except Exception:
            # The root process remains suspended/running only if job termination
            # itself failed; the explicit process-tree fallback avoids abandoning it.
            pass
    _terminate_process_tree(process, known_descendants)


class _OutputBudget:
    def __init__(self, *, per_stream_limit: int, total_limit: int) -> None:
        self._per_stream_limit = per_stream_limit
        self._total_limit = total_limit
        self._stream_sizes = {"stdout": 0, "stderr": 0}
        self._total_size = 0
        self._lock = threading.Lock()
        self.exceeded = threading.Event()

    def claim(self, stream_name: str, requested: int) -> int:
        with self._lock:
            allowed = min(
                requested,
                self._per_stream_limit - self._stream_sizes[stream_name],
                self._total_limit - self._total_size,
            )
            allowed = max(0, allowed)
            self._stream_sizes[stream_name] += allowed
            self._total_size += allowed
            if allowed < requested:
                self.exceeded.set()
            return allowed


def _drain_stream(
    pipe: BinaryIO,
    output_path: Path,
    stream_name: str,
    budget: _OutputBudget,
    errors: list[BaseException],
) -> None:
    try:
        with output_path.open("wb") as output_file:
            while chunk := pipe.read(8192):
                allowed = budget.claim(stream_name, len(chunk))
                if allowed:
                    output_file.write(chunk[:allowed])
    except BaseException as error:
        errors.append(error)


def _remember_descendants(
    root: psutil.Process | None,
    root_pid: int,
    root_started_at: float,
    tracked: dict[int, psutil.Process],
) -> None:
    candidates = ([root] if root is not None else []) + list(tracked.values())
    for candidate in candidates:
        if not _process_is_running(candidate):
            continue
        try:
            descendants = candidate.children(recursive=True)
        except psutil.Error:
            continue
        for descendant in descendants:
            tracked[descendant.pid] = descendant

    if os.name != "nt":
        return

    parent_ids = {root_pid, *tracked.keys()}
    process_snapshot: dict[int, tuple[int, psutil.Process]] = {}
    for candidate in psutil.process_iter(attrs=("pid", "ppid")):
        try:
            info = candidate.info
            pid = int(info["pid"])
            parent_pid = int(info["ppid"])
        except (KeyError, TypeError, ValueError, psutil.Error):
            continue
        if pid == root_pid:
            continue
        process_snapshot[pid] = (parent_pid, candidate)

    changed = True
    while changed:
        changed = False
        for pid, (parent_pid, candidate) in process_snapshot.items():
            if pid in parent_ids or parent_pid not in parent_ids:
                continue
            try:
                if candidate.create_time() < root_started_at:
                    continue
            except psutil.Error:
                continue
            tracked[pid] = candidate
            parent_ids.add(pid)
            changed = True


def _discard_stopped_processes(tracked: dict[int, psutil.Process]) -> bool:
    for pid, process in list(tracked.items()):
        if not _process_is_running(process):
            tracked.pop(pid, None)
    return bool(tracked)


def _process_is_running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _posix_process_group_is_running(process_group_id: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_posix_process_group_exit(
    process_group_id: int, timeout_seconds: float
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _posix_process_group_is_running(process_group_id):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    known_descendants: Sequence[psutil.Process] = (),
) -> None:
    descendants_by_pid = {child.pid: child for child in known_descendants}
    try:
        root = psutil.Process(process.pid)
        for descendant in root.children(recursive=True):
            descendants_by_pid[descendant.pid] = descendant
    except psutil.Error:
        root = None

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    elif process.poll() is None:
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        if taskkill.is_file():
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    targets = list(descendants_by_pid.values()) + (
        [root] if root is not None else []
    )
    for target in targets:
        try:
            target.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(targets, timeout=0.5)
    if os.name == "posix" and not _wait_for_posix_process_group_exit(
        process.pid, 0.5
    ):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for target in alive:
        try:
            target.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=2)
    _wait_for_process(process)


def _wait_for_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _ensure_no_links(
    path: Path,
    error_type: type[Exception] = RunnerConfigurationError,
) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part in {path.anchor, "", "."}:
            continue
        current = current / part
        try:
            status = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(status.st_mode) or bool(
            getattr(status, "st_file_attributes", 0) & 0x400
        ):
            raise error_type(
                "symbolic links and reparse points are forbidden"
            )


def _assert_inside(
    path: Path,
    root: Path,
    error_type: type[Exception] = RunnerConfigurationError,
    *,
    message: str = "path escapes its required workspace",
) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise error_type(message) from error


def _assert_disjoint_storage(
    storage: Path,
    workspace: Path,
    *,
    error_type: type[Exception] = RunnerConfigurationError,
) -> None:
    for candidate, root in ((storage, workspace), (workspace, storage)):
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        raise error_type("runner staging storage must be outside the suite workspace")
