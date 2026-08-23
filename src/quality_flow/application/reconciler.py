"""Reconcile expired worker leases with a controlled retry budget."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select

from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
from quality_flow.domain.state_machine import ensure_attempt_transition, ensure_run_transition
from quality_flow.infrastructure.database import SessionFactory
from quality_flow.infrastructure.models import OutboxEvent, Run, RunAttempt, RunEvent


def _allows_worker_lost_retry(run: Run, attempt: RunAttempt) -> bool:
    snapshot = run.suite_snapshot
    if type(snapshot) is not dict:
        return False
    raw = snapshot.get("retry_policy")
    if type(raw) is not dict or set(raw) != {"max_attempts", "retry_on"}:
        return False
    max_attempts = raw.get("max_attempts")
    retry_on = raw.get("retry_on")
    return (
        type(max_attempts) is int
        and max_attempts == 2
        and type(retry_on) is list
        and retry_on == ["worker_lost"]
        and type(attempt.attempt_no) is int
        and attempt.attempt_no == 1
    )


class LeaseReconciler:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        batch_size: int = 100,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._clock = clock or (lambda: datetime.now(UTC))

    def reconcile_once(self, *, now: datetime | None = None) -> int:
        reconciled_at = now or self._clock()
        with self._session_factory() as session:
            candidates = session.execute(
                select(
                    RunAttempt.run_id,
                    RunAttempt.attempt_id,
                    RunAttempt.lease_token,
                )
                .where(
                    RunAttempt.status == AttemptStatus.RUNNING,
                    RunAttempt.lease_expires_at <= reconciled_at,
                )
                .order_by(RunAttempt.lease_expires_at, RunAttempt.attempt_id)
                .limit(self._batch_size)
            ).all()

        reconciled = 0
        for run_id, attempt_id, candidate_lease_token in candidates:
            with self._session_factory.begin() as session:
                run = session.scalar(
                    select(Run)
                    .where(
                        Run.run_id == run_id,
                        Run.status == RunStatus.RUNNING,
                    )
                    .with_for_update(skip_locked=True)
                )
                if run is None:
                    continue
                attempt = session.scalar(
                    select(RunAttempt)
                    .where(
                        RunAttempt.run_id == run_id,
                        RunAttempt.attempt_id == attempt_id,
                    )
                    .with_for_update(skip_locked=True)
                )
                if (
                    run.status is not RunStatus.RUNNING
                    or attempt is None
                    or attempt.status is not AttemptStatus.RUNNING
                    or attempt.lease_token != candidate_lease_token
                    or attempt.lease_expires_at is None
                    or attempt.lease_expires_at > reconciled_at
                ):
                    continue

                ensure_attempt_transition(attempt.status, AttemptStatus.ABANDONED)
                attempt.status = AttemptStatus.ABANDONED
                attempt.finished_at = reconciled_at
                attempt.failure_reason = "worker lease expired"

                if _allows_worker_lost_retry(run, attempt):
                    ensure_run_transition(run.status, RunStatus.QUEUED)
                    run.status = RunStatus.QUEUED
                    run.outcome = RunOutcome.UNKNOWN
                    run.finished_at = None
                    run.updated_at = reconciled_at
                    session.add(
                        RunEvent(
                            run_id=run_id,
                            event_type="run.retry_scheduled",
                            payload={
                                "status": RunStatus.QUEUED.value,
                                "outcome": RunOutcome.UNKNOWN.value,
                                "attempt_no": attempt.attempt_no,
                                "next_attempt_no": attempt.attempt_no + 1,
                                "reason": "worker_lost",
                            },
                            created_at=reconciled_at,
                        )
                    )
                    session.add(
                        OutboxEvent(
                            aggregate_type="run",
                            aggregate_id=run_id,
                            event_type="run.retry_scheduled",
                            payload={"run_id": str(run_id)},
                            created_at=reconciled_at,
                        )
                    )
                else:
                    ensure_run_transition(run.status, RunStatus.INFRA_FAILED)
                    run.status = RunStatus.INFRA_FAILED
                    run.outcome = RunOutcome.UNKNOWN
                    run.finished_at = reconciled_at
                    run.updated_at = reconciled_at
                    session.add(
                        RunEvent(
                            run_id=run_id,
                            event_type="run.abandoned",
                            payload={
                                "status": RunStatus.INFRA_FAILED.value,
                                "outcome": RunOutcome.UNKNOWN.value,
                                "attempt_status": AttemptStatus.ABANDONED.value,
                                "attempt_id": str(attempt_id),
                            },
                            created_at=reconciled_at,
                        )
                    )
                reconciled += 1
        return reconciled
