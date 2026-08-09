from __future__ import annotations

import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from quality_flow.application.run_service import NewOutboxEvent, RunService
from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
from quality_flow.domain.state_machine import InvalidStateTransition
from quality_flow.infrastructure.database import (
    SqlAlchemyUnitOfWork,
    make_engine,
    make_session_factory,
)
from quality_flow.infrastructure.models import OutboxEvent, Run, RunAttempt, RunEvent
from quality_flow.infrastructure.repositories import RunRepository
from quality_flow.suites.registry import SuiteRegistry


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://quality_flow:quality_flow@localhost:5432/quality_flow",
)


@pytest.fixture(scope="module")
def session_factory():
    engine = make_engine(DATABASE_URL)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(session_factory) -> None:
    with session_factory.begin() as session:
        session.execute(delete(OutboxEvent))
        session.execute(delete(Run))


@pytest.fixture
def registry() -> SuiteRegistry:
    project_root = Path(__file__).resolve().parents[2]
    return SuiteRegistry.from_yaml(project_root / "config" / "suites.yaml", project_root)


def _counts(session_factory) -> tuple[int, int, int]:
    with session_factory() as session:
        return (
            session.scalar(select(func.count()).select_from(Run)) or 0,
            session.scalar(select(func.count()).select_from(RunEvent)) or 0,
            session.scalar(select(func.count()).select_from(OutboxEvent)) or 0,
        )


def test_duplicate_keys_persist_one_run_and_one_outbox_event(
    session_factory, registry: SuiteRegistry
) -> None:
    service = RunService(SqlAlchemyUnitOfWork(session_factory), registry)

    first = service.create_run("demo-api", "same-key", {"scenario": "smoke"})
    second = service.create_run("demo-api", "same-key", {"scenario": "smoke"})

    assert first.run_id == second.run_id
    assert _counts(session_factory) == (1, 1, 1)
    with session_factory() as session:
        stored = session.get(Run, first.run_id)
        assert stored is not None
        assert stored.status is RunStatus.QUEUED
        assert stored.parameters == {"scenario": "smoke"}
        assert stored.suite_snapshot["runner_type"] == "pytest"
        assert stored.gate_policy_snapshot["min_pass_rate"] == 1.0
        assert stored.outcome is RunOutcome.UNKNOWN
        assert stored.version == 1


class ConstraintFailingOutboxUnitOfWork(SqlAlchemyUnitOfWork):
    def add_outbox_event(self, event: NewOutboxEvent) -> None:
        self.session.add(
            OutboxEvent(
                outbox_event_id=event.outbox_event_id,
                aggregate_type=None,  # type: ignore[arg-type]
                aggregate_id=event.aggregate_id,
                event_type=event.event_type,
                payload=event.payload,
                created_at=event.created_at,
            )
        )


def test_commit_constraint_failure_rolls_back_run_event_and_outbox(
    session_factory, registry: SuiteRegistry
) -> None:
    service = RunService(ConstraintFailingOutboxUnitOfWork(session_factory), registry)

    with pytest.raises(IntegrityError):
        service.create_run("demo-api", "rollback-key", {"scenario": "smoke"})

    assert _counts(session_factory) == (0, 0, 0)


class BarrierRunRepository(RunRepository):
    def __init__(self, session, barrier: Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier
        self._initial_read_complete = False

    def get_by_idempotency_key(self, idempotency_key: str) -> Run | None:
        run = super().get_by_idempotency_key(idempotency_key)
        if not self._initial_read_complete:
            self._initial_read_complete = True
            self._barrier.wait(timeout=10)
        return run


class BarrierUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory, barrier: Barrier) -> None:
        super().__init__(session_factory)
        self._barrier = barrier

    def __enter__(self):
        super().__enter__()
        self.runs = BarrierRunRepository(self.session, self._barrier)
        return self


def test_concurrent_first_submission_returns_one_persisted_run(
    session_factory, registry: SuiteRegistry
) -> None:
    barrier = Barrier(2)

    def submit() -> Run:
        service = RunService(BarrierUnitOfWork(session_factory, barrier), registry)
        return service.create_run("demo-api", "concurrent-key", {"scenario": "smoke"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(submit)
        second_future = executor.submit(submit)
        first = first_future.result(timeout=15)
        second = second_future.result(timeout=15)

    assert first.run_id == second.run_id
    assert _counts(session_factory) == (1, 1, 1)


def test_repository_claims_queued_run_and_records_terminal_result(
    session_factory, registry: SuiteRegistry
) -> None:
    created = RunService(SqlAlchemyUnitOfWork(session_factory), registry).create_run(
        "demo-api", "claim-key", {"scenario": "regression"}
    )

    with SqlAlchemyUnitOfWork(session_factory) as uow:
        claimed = uow.runs.claim_queued_run(created.run_id)
        assert claimed is not None
        attempt_id = claimed.attempts[-1].attempt_id
        assert claimed.status is RunStatus.RUNNING
        assert claimed.attempts[-1].status is AttemptStatus.RUNNING
        uow.commit()

    with SqlAlchemyUnitOfWork(session_factory) as uow:
        terminal = uow.runs.record_terminal_result(
            created.run_id,
            attempt_id,
            AttemptStatus.PASSED,
            RunOutcome.PASSED,
        )
        uow.commit()

    assert terminal.status is RunStatus.COMPLETED
    assert terminal.outcome is RunOutcome.PASSED
    with session_factory() as session:
        attempt = session.get(RunAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status is AttemptStatus.PASSED


def test_claim_targets_requested_run_and_duplicate_delivery_is_a_no_op(
    session_factory, registry: SuiteRegistry
) -> None:
    service = RunService(SqlAlchemyUnitOfWork(session_factory), registry)
    first = service.create_run("demo-api", "claim-first", {"scenario": "smoke"})
    second = service.create_run("demo-api", "claim-second", {"scenario": "smoke"})

    with SqlAlchemyUnitOfWork(session_factory) as uow:
        claimed = uow.runs.claim_queued_run(second.run_id, worker_id="worker-2")
        uow.commit()

    assert claimed is not None
    assert claimed.run_id == second.run_id
    assert claimed.attempts[-1].worker_id == "worker-2"

    with SqlAlchemyUnitOfWork(session_factory) as uow:
        duplicate = uow.runs.claim_queued_run(second.run_id, worker_id="worker-duplicate")
        uow.commit()

    assert duplicate is None
    with session_factory() as session:
        assert session.get(Run, first.run_id).status is RunStatus.QUEUED
        stored_second = session.get(Run, second.run_id)
        assert stored_second.status is RunStatus.RUNNING
        assert len(stored_second.attempts) == 1


def test_schema_contains_task7_foundation_and_enum_checks(session_factory) -> None:
    inspector = inspect(session_factory.kw["bind"])
    run_columns = {column["name"]: column for column in inspector.get_columns("runs")}
    attempt_columns = {
        column["name"]: column for column in inspector.get_columns("run_attempts")
    }
    gate_columns = {
        column["name"] for column in inspector.get_columns("gate_evaluations")
    }

    assert "version" in run_columns
    assert run_columns["outcome"]["nullable"] is False
    assert {
        "lease_token",
        "heartbeat_at",
        "lease_expires_at",
        "exit_code",
        "failure_reason",
    } <= attempt_columns.keys()
    assert "run_id" not in gate_columns
    assert "attempt_id" in gate_columns

    run_checks = inspector.get_check_constraints("runs")
    attempt_checks = inspector.get_check_constraints("run_attempts")
    assert any("status" in check["sqltext"] for check in run_checks)
    assert any("outcome" in check["sqltext"] for check in run_checks)
    assert any("status" in check["sqltext"] for check in attempt_checks)


def test_run_version_detects_concurrent_updates(
    session_factory, registry: SuiteRegistry
) -> None:
    created = RunService(SqlAlchemyUnitOfWork(session_factory), registry).create_run(
        "demo-api", "version-key", {"scenario": "smoke"}
    )
    first_session = session_factory()
    second_session = session_factory()
    try:
        first = first_session.get(Run, created.run_id)
        second = second_session.get(Run, created.run_id)
        assert first is not None and second is not None
        first.suite_id = "first-update"
        first_session.commit()
        second.suite_id = "stale-update"
        with pytest.raises(StaleDataError):
            second_session.commit()
    finally:
        first_session.close()
        second_session.close()


def test_repository_rejects_untrusted_terminal_result_combination(
    session_factory, registry: SuiteRegistry
) -> None:
    created = RunService(SqlAlchemyUnitOfWork(session_factory), registry).create_run(
        "demo-api", "invalid-terminal-key", {"scenario": "smoke"}
    )
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        claimed = uow.runs.claim_queued_run(created.run_id)
        assert claimed is not None
        attempt_id = claimed.attempts[-1].attempt_id
        uow.commit()

    with pytest.raises(InvalidStateTransition):
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.record_terminal_result(
                created.run_id,
                attempt_id,
                AttemptStatus.TEST_FAILED,
                RunOutcome.PASSED,
            )
            uow.commit()

    with session_factory() as session:
        stored = session.get(Run, created.run_id)
        attempt = session.get(RunAttempt, attempt_id)
        assert stored is not None and attempt is not None
        assert stored.status is RunStatus.RUNNING
        assert stored.outcome is RunOutcome.UNKNOWN
        assert attempt.status is AttemptStatus.RUNNING
