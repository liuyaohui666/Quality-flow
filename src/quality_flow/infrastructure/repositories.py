"""PostgreSQL repositories for queued-run coordination and completion."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
from quality_flow.domain.state_machine import (
    ensure_attempt_transition,
    ensure_run_transition,
    resolve_terminal_run_status,
)
from quality_flow.infrastructure.models import Run, RunAttempt, RunEvent


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
        self, run_id: UUID, worker_id: str | None = None
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
        now = datetime.now(UTC)
        next_attempt = (run.attempts[-1].attempt_no + 1) if run.attempts else 1
        run.status = RunStatus.RUNNING
        run.started_at = now
        run.updated_at = now
        run.attempts.append(
            RunAttempt(
                attempt_no=next_attempt,
                status=AttemptStatus.RUNNING,
                worker_id=worker_id,
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

    def record_terminal_result(
        self,
        run_id: UUID,
        attempt_id: UUID,
        attempt_status: AttemptStatus,
        outcome: RunOutcome,
    ) -> Run:
        run = self._session.scalar(
            select(Run)
            .where(Run.run_id == run_id)
            .with_for_update()
            .options(selectinload(Run.attempts))
        )
        if run is None:
            raise LookupError(f"Unknown run: {run_id}")
        attempt = next(
            (candidate for candidate in run.attempts if candidate.attempt_id == attempt_id),
            None,
        )
        if attempt is None:
            raise LookupError(f"Unknown attempt for run {run_id}: {attempt_id}")

        terminal_run_status = resolve_terminal_run_status(attempt_status, outcome)
        ensure_attempt_transition(attempt.status, attempt_status)
        ensure_run_transition(run.status, terminal_run_status)

        now = datetime.now(UTC)
        attempt.status = attempt_status
        attempt.finished_at = now
        run.status = terminal_run_status
        run.outcome = outcome
        run.finished_at = now
        run.updated_at = now
        run.events.append(
            RunEvent(
                event_type="run.finished",
                payload={
                    "status": terminal_run_status.value,
                    "outcome": outcome.value,
                    "attempt_status": attempt_status.value,
                },
                created_at=now,
            )
        )
        self._session.flush()
        return run
