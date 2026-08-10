"""Worker orchestration around short, PostgreSQL-authoritative transactions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
from typing import Protocol
from uuid import UUID

from quality_flow.domain.enums import AttemptStatus, RunOutcome
from quality_flow.infrastructure.artifacts import ArtifactMetadata, FileArtifactStore
from quality_flow.infrastructure.database import SessionFactory, SqlAlchemyUnitOfWork
from quality_flow.runners.base import ExecutionSpec, RunnerOutcome
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
        lease_duration: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runners = dict(runners)
        self._artifact_store = artifact_store
        self._workspace_root = Path(workspace_root)
        self._lease_duration = lease_duration
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(self, run_id: UUID, *, worker_id: str | None = None) -> bool:
        claimed = self._claim(run_id, worker_id)
        if claimed is None:
            return False

        workspace = self._copy_attempt_workspace(claimed)
        runner = self._runners[claimed.runner_type]
        spec = ExecutionSpec(
            argv=claimed.argv,
            timeout_seconds=claimed.timeout_seconds,
            allowed_workspace_root=workspace.resolve(),
            parameters=claimed.parameters,
            gate_policy=claimed.gate_policy,
        )

        def heartbeat() -> None:
            with SqlAlchemyUnitOfWork(self._session_factory) as uow:
                uow.runs.heartbeat(
                    claimed.attempt_id,
                    claimed.lease_token,
                    now=self._clock(),
                    lease_duration=self._lease_duration,
                )
                uow.commit()

        outcome = runner.run(spec, workspace, heartbeat)
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
            _cleanup_staging_roots(outcome, workspace)

    def _claim(self, run_id: UUID, worker_id: str | None) -> ClaimedExecution | None:
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
            snapshot = dict(run.suite_snapshot)
            policy = GatePolicy(**dict(run.gate_policy_snapshot))
            source_directory = Path(snapshot["working_directory"])
            _validate_workspace_boundary(source_directory, self._workspace_root)
            claimed = ClaimedExecution(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                lease_token=attempt.lease_token,
                runner_type=snapshot["runner_type"],
                source_directory=source_directory,
                argv=tuple(snapshot["argv"]),
                timeout_seconds=snapshot["timeout_seconds"],
                parameters=dict(run.parameters),
                gate_policy=policy,
            )
            uow.commit()
            return claimed

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


def _cleanup_staging_roots(outcome: RunnerOutcome, workspace: Path) -> None:
    resolved_workspace = workspace.resolve()
    roots = {artifact.source_root.resolve() for artifact in outcome.artifacts}
    for root in roots:
        if root == resolved_workspace or resolved_workspace.is_relative_to(root):
            continue
        try:
            shutil.rmtree(root)
        except OSError:
            pass


def _validate_workspace_boundary(source: Path, workspace_root: Path) -> None:
    try:
        resolved_source = source.resolve(strict=True)
    except OSError as error:
        raise WorkerConfigurationError("suite source directory is unavailable") from error
    resolved_workspace_root = workspace_root.resolve()
    if (
        resolved_source == resolved_workspace_root
        or resolved_source.is_relative_to(resolved_workspace_root)
        or resolved_workspace_root.is_relative_to(resolved_source)
    ):
        raise WorkerConfigurationError(
            "attempt workspace root must be disjoint from the suite source"
        )
