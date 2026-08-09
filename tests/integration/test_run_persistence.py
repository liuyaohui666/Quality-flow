from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from quality_flow.application.run_service import NewOutboxEvent, RunService
from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
from quality_flow.infrastructure.database import (
    SqlAlchemyUnitOfWork,
    make_engine,
    make_session_factory,
)
from quality_flow.infrastructure.models import OutboxEvent, Run, RunAttempt
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


def _counts(session_factory) -> tuple[int, int]:
    with session_factory() as session:
        return (
            session.scalar(select(func.count()).select_from(Run)) or 0,
            session.scalar(select(func.count()).select_from(OutboxEvent)) or 0,
        )


def test_duplicate_keys_persist_one_run_and_one_outbox_event(
    session_factory, registry: SuiteRegistry
) -> None:
    service = RunService(SqlAlchemyUnitOfWork(session_factory), registry)

    first = service.create_run("demo-api", "same-key", {"scenario": "smoke"})
    second = service.create_run("demo-api", "same-key", {"scenario": "smoke"})

    assert first.run_id == second.run_id
    assert _counts(session_factory) == (1, 1)
    with session_factory() as session:
        stored = session.get(Run, first.run_id)
        assert stored is not None
        assert stored.status is RunStatus.QUEUED
        assert stored.parameters == {"scenario": "smoke"}
        assert stored.suite_snapshot["runner_type"] == "pytest"
        assert stored.gate_policy_snapshot["min_pass_rate"] == 1.0


class FailingOutboxUnitOfWork(SqlAlchemyUnitOfWork):
    def add_outbox_event(self, event: NewOutboxEvent) -> None:
        raise RuntimeError("forced outbox failure")


def test_forced_failure_rolls_back_run_and_outbox(
    session_factory, registry: SuiteRegistry
) -> None:
    service = RunService(FailingOutboxUnitOfWork(session_factory), registry)

    with pytest.raises(RuntimeError, match="forced outbox failure"):
        service.create_run("demo-api", "rollback-key", {"scenario": "smoke"})

    assert _counts(session_factory) == (0, 0)


def test_repository_claims_queued_run_and_records_terminal_result(
    session_factory, registry: SuiteRegistry
) -> None:
    created = RunService(SqlAlchemyUnitOfWork(session_factory), registry).create_run(
        "demo-api", "claim-key", {"scenario": "regression"}
    )

    with SqlAlchemyUnitOfWork(session_factory) as uow:
        claimed = uow.runs.claim_queued_run()
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
