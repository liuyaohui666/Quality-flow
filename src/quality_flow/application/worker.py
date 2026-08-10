"""Worker orchestration around short, PostgreSQL-authoritative transactions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
from typing import Protocol
from uuid import UUID

from quality_flow.domain.enums import AttemptStatus, RunOutcome
from quality_flow.infrastructure.artifacts import ArtifactMetadata, FileArtifactStore
from quality_flow.infrastructure.database import SessionFactory, SqlAlchemyUnitOfWork
from quality_flow.infrastructure.models import Run
from quality_flow.runners.base import ExecutionSpec, RunnerOutcome
from quality_flow.runners.subprocess_runner import RunnerConfigurationError
from quality_flow.suites.registry import GatePolicy


class Runner(Protocol):
    def run(
        self,
        spec: ExecutionSpec,
        workspace: Path,
        heartbeat: Callable[[], None],
    ) -> RunnerOutcome: ...


class WorkerConfigurationError(ValueError):
    """Worker-owned paths do not preserve the suite-source trust boundary."""


class WorkerSetupError(RuntimeError):
    """A claimed attempt cannot be prepared from its immutable snapshot."""


@dataclass(frozen=True)
class ClaimedLease:
    run_id: UUID
    attempt_id: UUID
    lease_token: UUID
    started_at: datetime


@dataclass(frozen=True)
class ClaimedExecution:
    run_id: UUID
    attempt_id: UUID
    lease_token: UUID
    runner_type: str
    source_directory: Path
    argv: tuple[str, ...]
    timeout_seconds: float
    parameters: Mapping[str, str]
    gate_policy: GatePolicy


class RunWorker:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        runners: Mapping[str, Runner],
        artifact_store: FileArtifactStore,
        workspace_root: Path,
        staging_root: Path,
        lease_duration: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runners = dict(runners)
        self._artifact_store = artifact_store
        self._workspace_root = Path(workspace_root)
        self._staging_root = Path(staging_root)
        self._lease_duration = lease_duration
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(self, run_id: UUID, *, worker_id: str | None = None) -> bool:
        lease = self._claim(run_id, worker_id)
        if lease is None:
            return False

        try:
            claimed, runner, workspace, spec = self._prepare_execution(lease)
        except WorkerSetupError as error:
            _cleanup_attempt_workspace(lease, self._workspace_root)
            self._record_setup_failure(lease, str(error))
            return True

        def heartbeat() -> None:
            with SqlAlchemyUnitOfWork(self._session_factory) as uow:
                uow.runs.heartbeat(
                    claimed.attempt_id,
                    claimed.lease_token,
                    now=self._clock(),
                    lease_duration=self._lease_duration,
                )
                uow.commit()

        try:
            outcome = runner.run(spec, workspace, heartbeat)
        except RunnerConfigurationError as error:
            self._record_setup_failure(lease, f"runner setup: {error}")
            return True

        outcome = _normalize_outcome(outcome)
        try:
            stored_artifacts = tuple(
                self._artifact_store.put(
                    artifact.source_path,
                    ArtifactMetadata(
                        run_id=claimed.run_id,
                        attempt_id=claimed.attempt_id,
                        artifact_type=artifact.artifact_type,
                        mime_type=artifact.mime_type,
                    ),
                    attempt_workspace=artifact.source_root,
                )
                for artifact in outcome.artifacts
            )
            persisted_outcome = _run_outcome(outcome)
            with SqlAlchemyUnitOfWork(self._session_factory) as uow:
                uow.runs.record_terminal_aggregate(
                    claimed.run_id,
                    claimed.attempt_id,
                    claimed.lease_token,
                    outcome,
                    persisted_outcome,
                    stored_artifacts,
                    now=self._clock(),
                )
                uow.commit()
            return True
        finally:
            _cleanup_staging_roots(outcome, self._staging_root)

    def _claim(self, run_id: UUID, worker_id: str | None) -> ClaimedLease | None:
        with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            run = uow.runs.claim_queued_run(
                run_id,
                worker_id,
                now=self._clock(),
                lease_duration=self._lease_duration,
            )
            if run is None:
                return None
            attempt = run.attempts[-1]
            lease = ClaimedLease(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                lease_token=attempt.lease_token,
                started_at=attempt.started_at,
            )
            uow.commit()
            return lease

    def _prepare_execution(
        self, lease: ClaimedLease
    ) -> tuple[ClaimedExecution, Runner, Path, ExecutionSpec]:
        with self._session_factory() as session:
            run = session.get(Run, lease.run_id)
            if run is None:
                raise WorkerSetupError("run snapshot is unavailable")
            try:
                snapshot = dict(run.suite_snapshot)
                policy = GatePolicy(**dict(run.gate_policy_snapshot))
                runner_type = snapshot["runner_type"]
                if not isinstance(runner_type, str) or not runner_type:
                    raise ValueError("runner_type must be a non-empty string")
                source_directory = Path(snapshot["working_directory"])
                argv_value = snapshot["argv"]
                if isinstance(argv_value, (str, bytes)):
                    raise ValueError("argv must be a sequence of arguments")
                claimed = ClaimedExecution(
                    run_id=lease.run_id,
                    attempt_id=lease.attempt_id,
                    lease_token=lease.lease_token,
                    runner_type=runner_type,
                    source_directory=source_directory,
                    argv=tuple(argv_value),
                    timeout_seconds=snapshot["timeout_seconds"],
                    parameters=dict(run.parameters),
                    gate_policy=policy,
                )
            except (KeyError, TypeError, ValueError) as error:
                raise WorkerSetupError(f"invalid run snapshot: {error}") from error

        try:
            _validate_workspace_boundary(
                claimed.source_directory, self._workspace_root
            )
        except WorkerConfigurationError as error:
            raise WorkerSetupError(str(error)) from error
        try:
            runner = self._runners[claimed.runner_type]
        except KeyError as error:
            raise WorkerSetupError(
                f"unsupported runner type: {claimed.runner_type}"
            ) from error
        try:
            workspace = self._copy_attempt_workspace(claimed)
            spec = ExecutionSpec(
                argv=claimed.argv,
                timeout_seconds=claimed.timeout_seconds,
                allowed_workspace_root=workspace.resolve(),
                parameters=claimed.parameters,
                gate_policy=claimed.gate_policy,
            )
        except (OSError, shutil.Error, TypeError, ValueError) as error:
            raise WorkerSetupError(
                f"attempt workspace or execution spec could not be prepared: {error}"
            ) from error
        return claimed, runner, workspace, spec

    def _record_setup_failure(self, lease: ClaimedLease, reason: str) -> None:
        failed_at = self._clock()
        outcome = RunnerOutcome(
            attempt_status=AttemptStatus.INFRA_FAILED,
            exit_code=None,
            started_at=lease.started_at,
            finished_at=failed_at,
            gate_result=None,
            failure_kind="worker_setup",
            failure_summary=f"worker setup failed: {reason}",
        )
        with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            uow.runs.record_terminal_aggregate(
                lease.run_id,
                lease.attempt_id,
                lease.lease_token,
                outcome,
                RunOutcome.UNKNOWN,
                (),
                now=failed_at,
            )
            uow.commit()

    def _copy_attempt_workspace(self, claimed: ClaimedExecution) -> Path:
        workspace = self._workspace_root / str(claimed.run_id) / str(claimed.attempt_id)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(claimed.source_directory, workspace)
        return workspace


def _run_outcome(outcome: RunnerOutcome) -> RunOutcome:
    if outcome.attempt_status is AttemptStatus.PASSED:
        if outcome.gate_result is not None and not outcome.gate_result.passed:
            return RunOutcome.FAILED
        return RunOutcome.PASSED
    if outcome.attempt_status is AttemptStatus.TEST_FAILED:
        return RunOutcome.FAILED
    return RunOutcome.UNKNOWN


def _normalize_outcome(outcome: RunnerOutcome) -> RunnerOutcome:
    if outcome.attempt_status is AttemptStatus.PASSED and outcome.gate_result is None:
        return replace(
            outcome,
            attempt_status=AttemptStatus.INFRA_FAILED,
            failure_kind="invalid_runner_result",
            failure_summary="runner reported passed without a gate evaluation",
        )
    return outcome


def _cleanup_staging_roots(outcome: RunnerOutcome, staging_root: Path) -> None:
    resolved_staging_root = staging_root.resolve()
    roots = {artifact.source_root.resolve() for artifact in outcome.artifacts}
    for root in roots:
        if root == resolved_staging_root or not root.is_relative_to(
            resolved_staging_root
        ):
            continue
        try:
            shutil.rmtree(root)
        except OSError:
            pass


def _cleanup_attempt_workspace(lease: ClaimedLease, workspace_root: Path) -> None:
    workspace = workspace_root / str(lease.run_id) / str(lease.attempt_id)
    try:
        shutil.rmtree(workspace)
    except OSError:
        pass


def _validate_workspace_boundary(source: Path, workspace_root: Path) -> None:
    try:
        resolved_source = source.resolve(strict=True)
    except OSError as error:
        raise WorkerConfigurationError("suite source directory is unavailable") from error
    if not resolved_source.is_dir():
        raise WorkerConfigurationError("suite source directory is unavailable")
    resolved_workspace_root = workspace_root.resolve()
    if (
        resolved_source == resolved_workspace_root
        or resolved_source.is_relative_to(resolved_workspace_root)
        or resolved_workspace_root.is_relative_to(resolved_source)
    ):
        raise WorkerConfigurationError(
            "attempt workspace root must be disjoint from the suite source"
        )
