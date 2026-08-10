"""PostgreSQL repositories for queued-run coordination and completion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
from quality_flow.domain.state_machine import (
    ensure_attempt_transition,
    ensure_run_transition,
    resolve_terminal_run_status,
)
from quality_flow.infrastructure.artifacts import StoredArtifact
from quality_flow.infrastructure.models import (
    Artifact,
    CaseResult,
    GateEvaluation,
    Metric,
    Run,
    RunAttempt,
    RunEvent,
)
from quality_flow.runners.base import RunnerOutcome


class LeaseLostError(RuntimeError):
    """The worker no longer owns a live running-attempt lease."""


class RunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: Run) -> None:
        self._session.add(run)

    def get_by_idempotency_key(self, idempotency_key: str) -> Run | None:
        return self._session.scalar(
            select(Run).where(Run.idempotency_key == idempotency_key)
        )

    def claim_queued_run(
        self,
        run_id: UUID,
        worker_id: str | None = None,
        *,
        now: datetime | None = None,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> Run | None:
        run = self._session.scalar(
            select(Run)
            .where(Run.run_id == run_id, Run.status == RunStatus.QUEUED)
            .with_for_update(skip_locked=True)
            .options(selectinload(Run.attempts))
        )
        if run is None:
            return None

        ensure_run_transition(run.status, RunStatus.RUNNING)
        now = now or datetime.now(UTC)
        next_attempt = (run.attempts[-1].attempt_no + 1) if run.attempts else 1
        run.status = RunStatus.RUNNING
        run.started_at = now
        run.updated_at = now
        run.attempts.append(
            RunAttempt(
                attempt_no=next_attempt,
                status=AttemptStatus.RUNNING,
                worker_id=worker_id,
                lease_token=uuid4(),
                heartbeat_at=now,
                lease_expires_at=now + lease_duration,
                created_at=now,
                started_at=now,
            )
        )
        run.events.append(
            RunEvent(
                event_type="run.started",
                payload={"status": RunStatus.RUNNING.value, "attempt_no": next_attempt},
                created_at=now,
            )
        )
        self._session.flush()
        return run

    def heartbeat(
        self,
        attempt_id: UUID,
        lease_token: UUID,
        *,
        now: datetime | None = None,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> None:
        heartbeat_at = now or datetime.now(UTC)
        result = self._session.execute(
            update(RunAttempt)
            .where(
                RunAttempt.attempt_id == attempt_id,
                RunAttempt.status == AttemptStatus.RUNNING,
                RunAttempt.lease_token == lease_token,
                RunAttempt.lease_expires_at > heartbeat_at,
            )
            .values(
                heartbeat_at=heartbeat_at,
                lease_expires_at=heartbeat_at + lease_duration,
            )
        )
        if result.rowcount != 1:
            raise LeaseLostError("attempt lease is missing, expired, or no longer running")

    def record_terminal_aggregate(
        self,
        run_id: UUID,
        attempt_id: UUID,
        lease_token: UUID,
        runner_outcome: RunnerOutcome,
        outcome: RunOutcome,
        artifacts: tuple[StoredArtifact, ...],
        *,
        now: datetime | None = None,
    ) -> Run:
        terminal_at = now or datetime.now(UTC)
        run, attempt = self._lock_live_attempt(
            run_id, attempt_id, lease_token, terminal_at
        )

        terminal_status = resolve_terminal_run_status(
            runner_outcome.attempt_status, outcome
        )
        ensure_attempt_transition(attempt.status, runner_outcome.attempt_status)
        ensure_run_transition(run.status, terminal_status)

        attempt.status = runner_outcome.attempt_status
        attempt.exit_code = runner_outcome.exit_code
        attempt.failure_reason = runner_outcome.failure_summary
        attempt.started_at = runner_outcome.started_at
        attempt.finished_at = runner_outcome.finished_at
        run.status = terminal_status
        run.outcome = outcome
        run.finished_at = runner_outcome.finished_at
        run.updated_at = terminal_at

        for case in runner_outcome.case_results:
            self._session.add(
                CaseResult(
                    attempt_id=attempt_id,
                    node_id=case.node_id,
                    status=case.status,
                    duration_ms=case.duration_ms,
                    message=case.message,
                    details={},
                    created_at=terminal_at,
                )
            )
        for name, value, unit in _outcome_metrics(runner_outcome):
            self._session.add(
                Metric(
                    attempt_id=attempt_id,
                    metric_name=name,
                    metric_value=value,
                    unit=unit,
                    details={},
                    created_at=terminal_at,
                )
            )
        for stored in artifacts:
            self._session.add(
                Artifact(
                    attempt_id=attempt_id,
                    artifact_type=stored.artifact_type,
                    uri=stored.uri,
                    checksum=stored.checksum,
                    artifact_metadata=dict(stored.metadata),
                    created_at=terminal_at,
                )
            )
        if runner_outcome.gate_result is not None:
            self._session.add(
                GateEvaluation(
                    attempt_id=attempt_id,
                    gate_type=(
                        "functional"
                        if runner_outcome.case_summary is not None
                        else "performance"
                    ),
                    passed=runner_outcome.gate_result.passed,
                    reason_codes=list(runner_outcome.gate_result.reason_codes),
                    details=dict(runner_outcome.gate_result.details),
                    created_at=terminal_at,
                )
            )
        self._session.add(
            RunEvent(
                run_id=run_id,
                event_type="run.finished",
                payload={
                    "status": terminal_status.value,
                    "outcome": outcome.value,
                    "attempt_status": runner_outcome.attempt_status.value,
                },
                created_at=terminal_at,
            )
        )
        self._session.flush()
        return run

    def record_terminal_result(
        self,
        run_id: UUID,
        attempt_id: UUID,
        lease_token: UUID,
        attempt_status: AttemptStatus,
        outcome: RunOutcome,
        *,
        now: datetime | None = None,
    ) -> Run:
        terminal_at = now or datetime.now(UTC)
        run, attempt = self._lock_live_attempt(
            run_id, attempt_id, lease_token, terminal_at
        )

        terminal_run_status = resolve_terminal_run_status(attempt_status, outcome)
        ensure_attempt_transition(attempt.status, attempt_status)
        ensure_run_transition(run.status, terminal_run_status)

        attempt.status = attempt_status
        attempt.finished_at = terminal_at
        run.status = terminal_run_status
        run.outcome = outcome
        run.finished_at = terminal_at
        run.updated_at = terminal_at
        self._session.add(
            RunEvent(
                run_id=run_id,
                event_type="run.finished",
                payload={
                    "status": terminal_run_status.value,
                    "outcome": outcome.value,
                    "attempt_status": attempt_status.value,
                },
                created_at=terminal_at,
            )
        )
        self._session.flush()
        return run

    def _lock_live_attempt(
        self,
        run_id: UUID,
        attempt_id: UUID,
        lease_token: UUID,
        checked_at: datetime,
    ) -> tuple[Run, RunAttempt]:
        """Lock Run then Attempt and prove the caller still owns the live lease."""
        run = self._session.scalar(
            select(Run).where(Run.run_id == run_id).with_for_update()
        )
        if run is None:
            raise LeaseLostError("run lease owner no longer exists")
        attempt = self._session.scalar(
            select(RunAttempt)
            .where(
                RunAttempt.run_id == run_id,
                RunAttempt.attempt_id == attempt_id,
            )
            .with_for_update()
        )
        if (
            run.status is not RunStatus.RUNNING
            or attempt is None
            or attempt.status is not AttemptStatus.RUNNING
            or attempt.lease_token != lease_token
            or attempt.lease_expires_at is None
            or attempt.lease_expires_at <= checked_at
        ):
            raise LeaseLostError("attempt lease is missing, expired, or no longer running")
        return run, attempt


def _outcome_metrics(outcome: RunnerOutcome) -> tuple[tuple[str, float, str], ...]:
    metrics: list[tuple[str, float, str]] = []
    if outcome.case_summary is not None:
        summary = outcome.case_summary
        metrics.extend(
            (
                ("cases_total", float(summary.total), "count"),
                ("cases_passed", float(summary.passed), "count"),
                ("cases_failed", float(summary.failed), "count"),
                ("cases_errors", float(summary.errors), "count"),
                ("cases_skipped", float(summary.skipped), "count"),
            )
        )
    if outcome.performance_summary is not None:
        summary = outcome.performance_summary
        metrics.extend(
            (
                ("request_count", float(summary.request_count), "count"),
                ("p95_ms", summary.p95_ms, "ms"),
                ("failure_ratio", summary.failure_ratio, "ratio"),
                ("requests_per_second", summary.requests_per_second, "requests/s"),
                ("average_response_time_ms", summary.average_response_time_ms, "ms"),
                ("failure_count", float(summary.failure_count), "count"),
            )
        )
    return tuple(metrics)
