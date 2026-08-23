from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
from threading import Barrier, Event, Lock, enumerate as enumerate_threads
import time
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql
from redis import Redis
from sqlalchemy import create_engine, delete, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from quality_flow.application.dispatcher import OutboxDispatcher
from quality_flow.application.reconciler import LeaseReconciler
from quality_flow.application.run_service import RunService
from quality_flow.application.worker import RunWorker, _PostRunLeaseKeeper
from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
from quality_flow.infrastructure.artifacts import (
    ArtifactMetadata,
    ArtifactStoreError,
    FileArtifactStore,
)
from quality_flow.infrastructure.celery_app import CeleryRunPublisher, create_celery_app
from quality_flow.infrastructure.database import (
    SqlAlchemyUnitOfWork,
    make_engine,
    make_session_factory,
)
from quality_flow.infrastructure.models import (
    Artifact,
    CaseResult,
    GateEvaluation,
    Metric,
    OutboxEvent,
    Run,
    RunAttempt,
    RunEvent,
)
from quality_flow.infrastructure.outbox import SqlAlchemyOutboxStore
from quality_flow.infrastructure.repositories import LeaseLostError
from quality_flow.runners.base import (
    CaseResultData,
    CaseSummary,
    GateResult,
    PerformanceSummary,
    RunnerArtifact,
    RunnerOutcome,
)
from quality_flow.runners.subprocess_runner import RunnerConfigurationError
from quality_flow.suites.registry import SuiteRegistry


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://quality_flow@127.0.0.1:55432/quality_flow",
)
DEFAULT_ADMIN_DATABASE_URL = (
    "postgresql+psycopg://quality_flow@127.0.0.1:55432/postgres"
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


def test_real_postgres_dispatch_marks_only_after_publish_returns(
    session_factory, registry: SuiteRegistry
) -> None:
    created = RunService(SqlAlchemyUnitOfWork(session_factory), registry).create_run(
        "demo-api", "dispatch-real-postgres", {"scenario": "ok"}
    )
    store = SqlAlchemyOutboxStore(session_factory)
    observed = []

    def publish(*, event_id, run_id) -> None:
        with session_factory() as session:
            row = session.scalar(
                select(OutboxEvent).where(OutboxEvent.outbox_event_id == event_id)
            )
            observed.append((run_id, row.published_at, row.publish_attempts))

    dispatcher = OutboxDispatcher(
        store,
        publish,
        clock=lambda: datetime(2026, 8, 10, 2, 0, tzinfo=UTC),
    )

    assert dispatcher.dispatch_once() == 1
    with session_factory() as session:
        event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == created.run_id)
        )
        assert event.published_at == datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
        assert event.publish_attempts == 1
    assert observed == [(created.run_id, None, 1)]


def test_real_postgres_publish_failure_keeps_outbox_pending_and_counted(
    session_factory, registry: SuiteRegistry
) -> None:
    created = RunService(SqlAlchemyUnitOfWork(session_factory), registry).create_run(
        "demo-api", "dispatch-real-failure", {"scenario": "ok"}
    )

    def fail_publish(*, event_id, run_id) -> None:
        raise ConnectionError("isolated broker failure")

    assert OutboxDispatcher(
        SqlAlchemyOutboxStore(session_factory), fail_publish
    ).dispatch_once() == 0

    with session_factory() as session:
        event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == created.run_id)
        )
        assert event.published_at is None
        assert event.publish_attempts == 1


def _create_snapshot_run(
    session_factory,
    source: Path,
    key: str,
    *,
    retry_policy: dict[str, object] | None = None,
):
    run_id = uuid4()
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    suite_snapshot = {
        "suite_id": "snapshot-suite",
        "runner_type": "pytest",
        "working_directory": str(source),
        "argv": ["python", "-m", "pytest", "suite.py"],
        "timeout_seconds": 5,
        "allowed_parameters": {"scenario": ["stored"]},
        "source_revision": "immutable-revision",
    }
    if retry_policy is not None:
        suite_snapshot["retry_policy"] = retry_policy
    with session_factory.begin() as session:
        session.add(
            Run(
                run_id=run_id,
                suite_id="snapshot-suite",
                idempotency_key=key,
                parameters={"scenario": "stored"},
                suite_snapshot=suite_snapshot,
                gate_policy_snapshot={
                    "min_pass_rate": 1.0,
                    "max_failures": 0,
                    "max_error_rate": None,
                    "max_p95_ms": None,
                    "min_requests": None,
                },
                status=RunStatus.QUEUED,
                outcome=RunOutcome.UNKNOWN,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
    return run_id


class BlockingPassingRunner:
    def __init__(self, *, finished_at: datetime | None = None) -> None:
        self.started = Event()
        self.release = Event()
        self.calls = []
        self.finished_at = finished_at or datetime(
            2026, 8, 10, 2, 0, 2, tzinfo=UTC
        )

    def run(self, spec, workspace, heartbeat):
        self.calls.append((spec, workspace, heartbeat))
        self.started.set()
        assert self.release.wait(timeout=10)
        now = self.finished_at
        return RunnerOutcome(
            attempt_status=AttemptStatus.PASSED,
            exit_code=0,
            started_at=now - timedelta(seconds=1),
            finished_at=now,
            gate_result=GateResult(True, (), {"pass_rate": 1.0}),
        )


class BlockingPostprocessRunner:
    """Model an adapter blocked after its subprocess exits but before parsing ends."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self.process_finished = Event()
        self.release_postprocess = Event()

    def run(self, spec, workspace, heartbeat):
        heartbeat()
        self.process_finished.set()
        assert self.release_postprocess.wait(timeout=10)
        finished_at = self._clock()
        return RunnerOutcome(
            attempt_status=AttemptStatus.PASSED,
            exit_code=0,
            started_at=finished_at - timedelta(milliseconds=100),
            finished_at=finished_at,
            gate_result=GateResult(True, (), {"pass_rate": 1.0}),
        )


def test_concurrent_duplicate_delivery_claims_exact_run_once_with_valid_lease(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "suite.py").write_text("# immutable source\n", encoding="utf-8")
    untouched_id = _create_snapshot_run(session_factory, source, "untouched-run")
    run_id = _create_snapshot_run(session_factory, source, "duplicate-run")
    runner = BlockingPassingRunner()
    worker = RunWorker(
        session_factory,
        runners={"pytest": runner},
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        workspace_root=tmp_path / "workspaces",
        staging_root=tmp_path / "staging",
        lease_duration=timedelta(seconds=30),
        clock=lambda: datetime(2026, 8, 10, 2, 0, tzinfo=UTC),
    )

    simultaneous_start = Barrier(3)

    def deliver(worker_id: str) -> bool:
        simultaneous_start.wait(timeout=10)
        return worker.execute(run_id, worker_id=worker_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        deliveries = [
            executor.submit(deliver, "worker-a"),
            executor.submit(deliver, "worker-b"),
        ]
        simultaneous_start.wait(timeout=10)
        assert runner.started.wait(timeout=10)
        with session_factory() as session:
            running = session.get(Run, run_id)
            untouched = session.get(Run, untouched_id)
            attempt = session.scalar(
                select(RunAttempt).where(RunAttempt.run_id == run_id)
            )
            assert running.status is RunStatus.RUNNING
            assert untouched.status is RunStatus.QUEUED
            assert attempt.status is AttemptStatus.RUNNING
            assert attempt.attempt_no == 1
            assert attempt.worker_id in {"worker-a", "worker-b"}
            assert attempt.lease_token is not None
            assert attempt.heartbeat_at == datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
            assert attempt.lease_expires_at == datetime(
                2026, 8, 10, 2, 0, 30, tzinfo=UTC
            )
        runner.release.set()
        assert sorted(delivery.result(timeout=10) for delivery in deliveries) == [
            False,
            True,
        ]

    assert len(runner.calls) == 1
    with session_factory() as session:
        assert len(
            session.scalars(
                select(RunAttempt).where(RunAttempt.run_id == run_id)
            ).all()
        ) == 1


def test_concurrent_duplicate_retry_deliveries_claim_attempt_two_once(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "suite.py").write_text("# immutable source\n", encoding="utf-8")
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "duplicate-retry-delivery",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    first_claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=first_claimed_at, lease_duration=timedelta(seconds=5)
        )
        first_attempt_id = run.attempts[-1].attempt_id
        first_expiry = run.attempts[-1].lease_expires_at
        uow.commit()

    assert LeaseReconciler(session_factory).reconcile_once(now=first_expiry) == 1

    second_claimed_at = first_expiry + timedelta(seconds=1)
    runner = BlockingPassingRunner(
        finished_at=second_claimed_at + timedelta(seconds=2)
    )
    worker = RunWorker(
        session_factory,
        runners={"pytest": runner},
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        workspace_root=tmp_path / "workspaces",
        staging_root=tmp_path / "staging",
        lease_duration=timedelta(seconds=30),
        clock=lambda: second_claimed_at,
    )
    simultaneous_delivery = Barrier(3)

    def deliver(worker_id: str) -> bool:
        simultaneous_delivery.wait(timeout=10)
        return worker.execute(run_id, worker_id=worker_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        deliveries = [
            executor.submit(deliver, "stale-request-delivery"),
            executor.submit(deliver, "retry-event-delivery"),
        ]
        simultaneous_delivery.wait(timeout=10)
        assert runner.started.wait(timeout=10)

        with session_factory() as session:
            run = session.get(Run, run_id)
            attempts = session.scalars(
                select(RunAttempt)
                .where(RunAttempt.run_id == run_id)
                .order_by(RunAttempt.attempt_no)
            ).all()
            assert run.status is RunStatus.RUNNING
            assert run.started_at == first_claimed_at
            assert [attempt.attempt_no for attempt in attempts] == [1, 2]
            assert [attempt.status for attempt in attempts] == [
                AttemptStatus.ABANDONED,
                AttemptStatus.RUNNING,
            ]
            assert attempts[0].attempt_id == first_attempt_id
            assert attempts[1].worker_id in {
                "stale-request-delivery",
                "retry-event-delivery",
            }

        runner.release.set()
        assert sorted(delivery.result(timeout=10) for delivery in deliveries) == [
            False,
            True,
        ]

    assert len(runner.calls) == 1
    with session_factory() as session:
        attempts = session.scalars(
            select(RunAttempt)
            .where(RunAttempt.run_id == run_id)
            .order_by(RunAttempt.attempt_no)
        ).all()
        assert len(attempts) == 2
        assert attempts[1].status is AttemptStatus.PASSED


def test_stale_reconciler_candidate_does_not_lock_a_requeued_run(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "stale-reconciler-candidate",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=5)
        )
        reconcile_at = run.attempts[-1].lease_expires_at
        uow.commit()

    stale_before_run_lock = Event()
    release_stale_run_query = Event()
    stale_locked_requeued_run = Event()

    class StaleCandidateSession(Session):
        def scalar(self, statement, *args, **kwargs):
            rendered = str(statement)
            if "FROM runs" in rendered and "FOR UPDATE" in rendered:
                stale_before_run_lock.set()
                assert release_stale_run_query.wait(timeout=10)
                selected = super().scalar(statement, *args, **kwargs)
                if selected is not None and selected.status is RunStatus.QUEUED:
                    stale_locked_requeued_run.set()
                return selected
            return super().scalar(statement, *args, **kwargs)

    stale_factory = sessionmaker(
        bind=session_factory.kw["bind"],
        class_=StaleCandidateSession,
        expire_on_commit=False,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_reconcile = executor.submit(
            LeaseReconciler(stale_factory).reconcile_once,
            now=reconcile_at,
        )
        assert stale_before_run_lock.wait(timeout=10)
        assert LeaseReconciler(session_factory).reconcile_once(now=reconcile_at) == 1
        release_stale_run_query.set()
        assert stale_reconcile.result(timeout=10) == 0

    assert stale_locked_requeued_run.is_set() is False


def test_only_published_retry_delivery_waits_for_run_lock_and_claims_attempt_two(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "suite.py").write_text("# immutable source\n", encoding="utf-8")
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "published-retry-delivery",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    first_claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=first_claimed_at, lease_duration=timedelta(seconds=5)
        )
        first_expiry = run.attempts[-1].lease_expires_at
        uow.commit()
    assert LeaseReconciler(session_factory).reconcile_once(now=first_expiry) == 1

    published_messages: list[tuple[object, object]] = []

    def publish(*, event_id, run_id) -> None:
        published_messages.append((event_id, run_id))

    published_at = first_expiry + timedelta(milliseconds=100)
    assert OutboxDispatcher(
        SqlAlchemyOutboxStore(session_factory),
        publish,
        clock=lambda: published_at,
    ).dispatch_once() == 1

    claim_query_started = Event()
    claim_query_returned = Event()

    class SignalingClaimSession(Session):
        def scalar(self, statement, *args, **kwargs):
            rendered = str(statement)
            if "FROM runs" in rendered and "FOR UPDATE" in rendered:
                claim_query_started.set()
                selected = super().scalar(statement, *args, **kwargs)
                claim_query_returned.set()
                return selected
            return super().scalar(statement, *args, **kwargs)

    delivery_factory = sessionmaker(
        bind=session_factory.kw["bind"],
        class_=SignalingClaimSession,
        expire_on_commit=False,
    )
    delivered_at = first_expiry + timedelta(seconds=1)
    runner = BlockingPassingRunner(
        finished_at=delivered_at + timedelta(seconds=1)
    )
    runner.release.set()
    worker = RunWorker(
        delivery_factory,
        runners={"pytest": runner},
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        workspace_root=tmp_path / "workspaces",
        staging_root=tmp_path / "staging",
        lease_duration=timedelta(seconds=30),
        clock=lambda: delivered_at,
    )

    lock_session = session_factory()
    delivery_result = None
    returned_while_locked = None
    try:
        lock_session.begin()
        locked = lock_session.scalar(
            select(Run).where(Run.run_id == run_id).with_for_update()
        )
        assert locked.status is RunStatus.QUEUED

        with ThreadPoolExecutor(max_workers=1) as executor:
            delivery = executor.submit(
                worker.execute,
                run_id,
                worker_id="only-published-retry-delivery",
            )
            assert claim_query_started.wait(timeout=10)
            returned_while_locked = claim_query_returned.wait(timeout=0.5)
            lock_session.commit()
            delivery_result = delivery.result(timeout=10)
    finally:
        if lock_session.in_transaction():
            lock_session.rollback()
        lock_session.close()

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempts = session.scalars(
            select(RunAttempt)
            .where(RunAttempt.run_id == run_id)
            .order_by(RunAttempt.attempt_no)
        ).all()
        retry_outbox = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "run.retry_scheduled",
            )
        ).all()

        assert returned_while_locked is False
        assert delivery_result is True
        assert run.status is RunStatus.COMPLETED
        assert [attempt.status for attempt in attempts] == [
            AttemptStatus.ABANDONED,
            AttemptStatus.PASSED,
        ]
        assert len(retry_outbox) == 1
        assert retry_outbox[0].published_at == published_at
        assert published_messages == [(retry_outbox[0].outbox_event_id, run_id)]


def test_worker_terminalizes_attempt_workspace_root_overlapping_suite_source(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "overlapping-workspace")
    worker = RunWorker(
        session_factory,
        runners={},
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        workspace_root=source / "nested-runtime",
        staging_root=tmp_path / "staging",
    )

    assert worker.execute(run_id) is True

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(
            select(RunAttempt).where(RunAttempt.run_id == run_id)
        )
        assert (run.status, run.outcome) == (
            RunStatus.INFRA_FAILED,
            RunOutcome.UNKNOWN,
        )
        assert attempt.status is AttemptStatus.INFRA_FAILED
        assert "workspace root" in attempt.failure_reason
        assert len(
            session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "run.finished",
                )
            ).all()
        ) == 1


def _assert_single_infra_terminal(session_factory, run_id, reason: str) -> None:
    with session_factory() as session:
        run = session.get(Run, run_id)
        attempts = session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == run_id)
        ).all()
        events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.finished",
            )
        ).all()
        assert (run.status, run.outcome) == (
            RunStatus.INFRA_FAILED,
            RunOutcome.UNKNOWN,
        )
        assert len(attempts) == 1
        assert attempts[0].status is AttemptStatus.INFRA_FAILED
        assert reason in attempts[0].failure_reason
        assert len(events) == 1


class MisconfiguredRunner:
    def __init__(self) -> None:
        self.workspace: Path | None = None

    def run(self, spec, workspace, heartbeat):
        self.workspace = workspace
        raise RunnerConfigurationError("deliberately invalid runner setup")


def test_claimed_worker_terminalizes_invalid_snapshot_source_and_runner_setup(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()

    invalid_policy_id = _create_snapshot_run(
        session_factory, source, "invalid-policy-snapshot"
    )
    with session_factory.begin() as session:
        invalid_policy = session.get(Run, invalid_policy_id)
        invalid_policy.gate_policy_snapshot = {"unexpected_policy": True}

    missing_source = tmp_path / "missing-source"
    missing_source.mkdir()
    missing_source_id = _create_snapshot_run(
        session_factory, missing_source, "missing-source-snapshot"
    )
    missing_source.rmdir()

    unknown_runner_id = _create_snapshot_run(
        session_factory, source, "unknown-runner-snapshot"
    )
    with session_factory.begin() as session:
        unknown_runner = session.get(Run, unknown_runner_id)
        snapshot = dict(unknown_runner.suite_snapshot)
        snapshot["runner_type"] = "not-registered"
        unknown_runner.suite_snapshot = snapshot

    runner_setup_id = _create_snapshot_run(
        session_factory, source, "runner-configuration"
    )
    runner = MisconfiguredRunner()
    worker = RunWorker(
        session_factory,
        runners={"pytest": runner},
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        workspace_root=tmp_path / "workspaces",
        staging_root=tmp_path / "staging",
        clock=lambda: datetime(2026, 8, 10, 2, 0, tzinfo=UTC),
    )

    assert worker.execute(invalid_policy_id) is True
    assert worker.execute(missing_source_id) is True
    assert worker.execute(unknown_runner_id) is True
    assert worker.execute(runner_setup_id) is True

    _assert_single_infra_terminal(session_factory, invalid_policy_id, "snapshot")
    _assert_single_infra_terminal(session_factory, missing_source_id, "source")
    _assert_single_infra_terminal(session_factory, unknown_runner_id, "runner type")
    _assert_single_infra_terminal(session_factory, runner_setup_id, "runner setup")
    assert runner.workspace is not None
    assert not runner.workspace.exists()
    assert not runner.workspace.parent.exists()


class UnexpectedFailureRunner:
    def __init__(self) -> None:
        self.workspace: Path | None = None

    def run(self, spec, workspace, heartbeat):
        self.workspace = workspace
        raise RuntimeError("unexpected runner failure")


def test_worker_cleans_attempt_workspace_when_runner_raises_unexpectedly(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "unexpected-runner-error")
    runner = UnexpectedFailureRunner()
    worker = RunWorker(
        session_factory,
        runners={"pytest": runner},
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        workspace_root=tmp_path / "workspaces",
        staging_root=tmp_path / "staging",
    )

    with pytest.raises(RuntimeError, match="unexpected runner failure"):
        worker.execute(run_id)

    assert runner.workspace is not None
    assert not runner.workspace.exists()


class PassedWithoutGateRunner:
    def run(self, spec, workspace, heartbeat):
        now = datetime(2026, 8, 10, 2, 0, 2, tzinfo=UTC)
        return RunnerOutcome(
            attempt_status=AttemptStatus.PASSED,
            exit_code=0,
            started_at=now - timedelta(seconds=1),
            finished_at=now,
            gate_result=None,
        )


def test_worker_never_persists_passed_without_gate_evaluation(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "passed-without-gate")
    worker = RunWorker(
        session_factory,
        runners={"pytest": PassedWithoutGateRunner()},
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        workspace_root=tmp_path / "workspaces",
        staging_root=tmp_path / "staging",
        clock=lambda: datetime(2026, 8, 10, 2, 0, 3, tzinfo=UTC),
    )

    assert worker.execute(run_id) is True
    _assert_single_infra_terminal(
        session_factory, run_id, "passed without a gate evaluation"
    )


def test_heartbeat_is_fenced_by_running_status_token_and_unexpired_lease(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "heartbeat-fencing")
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id,
            "worker-a",
            now=claimed_at,
            lease_duration=timedelta(seconds=30),
        )
        attempt_id = run.attempts[-1].attempt_id
        token = run.attempts[-1].lease_token
        uow.commit()

    with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.runs.heartbeat(
            attempt_id,
            token,
            now=claimed_at + timedelta(seconds=5),
            lease_duration=timedelta(seconds=30),
        )
        uow.commit()
    with session_factory() as session:
        attempt = session.get(RunAttempt, attempt_id)
        assert attempt.heartbeat_at == claimed_at + timedelta(seconds=5)
        assert attempt.lease_expires_at == claimed_at + timedelta(seconds=35)

    with pytest.raises(LeaseLostError, match="lease"):
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.heartbeat(
                attempt_id,
                uuid4(),
                now=claimed_at + timedelta(seconds=6),
                lease_duration=timedelta(seconds=30),
            )

    with pytest.raises(LeaseLostError, match="lease"):
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.heartbeat(
                attempt_id,
                token,
                now=claimed_at + timedelta(seconds=36),
                lease_duration=timedelta(seconds=30),
            )

    terminal = RunnerOutcome(
        attempt_status=AttemptStatus.PASSED,
        exit_code=0,
        started_at=claimed_at,
        finished_at=claimed_at + timedelta(seconds=6),
        gate_result=GateResult(True, (), {}),
    )
    with pytest.raises(LeaseLostError, match="lease"):
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.record_terminal_aggregate(
                run_id,
                attempt_id,
                uuid4(),
                terminal,
                RunOutcome.PASSED,
                (),
                now=claimed_at + timedelta(seconds=6),
            )
    with pytest.raises(LeaseLostError, match="lease"):
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.record_terminal_aggregate(
                run_id,
                attempt_id,
                token,
                terminal,
                RunOutcome.PASSED,
                (),
                now=claimed_at + timedelta(seconds=36),
            )

    with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.runs.record_terminal_result(
            run_id,
            attempt_id,
            token,
            AttemptStatus.PASSED,
            RunOutcome.PASSED,
            now=claimed_at + timedelta(seconds=10),
        )
        uow.commit()
    with pytest.raises(LeaseLostError, match="lease"):
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.heartbeat(
                attempt_id,
                token,
                now=claimed_at + timedelta(seconds=10),
                lease_duration=timedelta(seconds=30),
            )


def test_worker_keeps_short_lease_during_runner_postprocess(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "suite.py").write_text("# immutable source\n", encoding="utf-8")
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "runner-postprocess-lease",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    initial_time = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    clock = MutableClock(initial_time)
    runner = BlockingPostprocessRunner(clock)
    worker = RunWorker(
        session_factory,
        runners={"pytest": runner},
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        workspace_root=tmp_path / "workspaces",
        staging_root=tmp_path / "staging",
        lease_duration=timedelta(milliseconds=300),
        clock=clock,
    )

    heartbeat_extended = False
    execution_result = None
    execution_error = None
    reconciled = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        execution = executor.submit(worker.execute, run_id)
        assert runner.process_finished.wait(timeout=10)
        heartbeat_time = initial_time + timedelta(milliseconds=200)
        clock.set(heartbeat_time)
        heartbeat_deadline = time.monotonic() + 2
        while time.monotonic() < heartbeat_deadline:
            with session_factory() as session:
                attempt = session.scalar(
                    select(RunAttempt).where(RunAttempt.run_id == run_id)
                )
                if (
                    attempt.heartbeat_at == heartbeat_time
                    and attempt.lease_expires_at
                    == heartbeat_time + timedelta(milliseconds=300)
                ):
                    heartbeat_extended = True
                    break
            time.sleep(0.01)

        reconciled_at = initial_time + timedelta(milliseconds=400)
        clock.set(reconciled_at)
        reconciled = LeaseReconciler(session_factory).reconcile_once(
            now=reconciled_at
        )
        runner.release_postprocess.set()
        try:
            execution_result = execution.result(timeout=10)
        except BaseException as error:
            execution_error = error

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempts = session.scalars(
            select(RunAttempt)
            .where(RunAttempt.run_id == run_id)
            .order_by(RunAttempt.attempt_no)
        ).all()
        retry_events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.retry_scheduled",
            )
        ).all()

        assert heartbeat_extended is True
        assert reconciled == 0
        assert execution_error is None
        assert execution_result is True
        assert run.status is RunStatus.COMPLETED
        assert [attempt.status for attempt in attempts] == [AttemptStatus.PASSED]
        assert retry_events == []


def test_terminal_run_lock_is_released_while_keeper_heartbeat_is_blocked(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "terminal-bounded-keeper-stop",
    )
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=30)
        )
        attempt_id = run.attempts[-1].attempt_id
        lease_token = run.attempts[-1].lease_token
        uow.commit()

    callback_started = Event()
    release_callback = Event()
    count_lock = Lock()
    call_count = 0

    def heartbeat() -> None:
        nonlocal call_count
        with count_lock:
            call_count += 1
            current_call = call_count
        if current_call > 1:
            callback_started.set()
            release_callback.wait()

    keeper = _PostRunLeaseKeeper(
        heartbeat,
        interval_seconds=0.01,
        thread_name="test-terminal-bounded-keeper-stop",
    )
    keeper.__enter__()
    assert callback_started.wait(timeout=1)

    terminal = RunnerOutcome(
        attempt_status=AttemptStatus.PASSED,
        exit_code=0,
        started_at=claimed_at,
        finished_at=claimed_at + timedelta(seconds=1),
        gate_result=GateResult(True, (), {}),
    )
    run_locked = Event()

    def stop_after_run_lock() -> None:
        run_locked.set()
        keeper.stop()

    def finalize() -> None:
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.record_terminal_aggregate(
                run_id,
                attempt_id,
                lease_token,
                terminal,
                RunOutcome.PASSED,
                (),
                now=claimed_at + timedelta(seconds=2),
                on_run_locked=stop_after_run_lock,
            )
            uow.commit()

    lock_error = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        final_write = executor.submit(finalize)
        assert run_locked.wait(timeout=10)
        try:
            with session_factory.begin() as session:
                session.execute(text("SET LOCAL lock_timeout = '300ms'"))
                session.scalar(
                    select(Run).where(Run.run_id == run_id).with_for_update()
                )
        except BaseException as error:
            lock_error = error
        finally:
            release_callback.set()
            final_write.result(timeout=10)

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        assert lock_error is None
        assert run.status is RunStatus.COMPLETED
        assert attempt.status is AttemptStatus.PASSED


def test_terminal_attempt_lock_timeout_rolls_back_run_lock_before_holder_releases(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "terminal-attempt-lock-timeout",
    )
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=30)
        )
        attempt_id = run.attempts[-1].attempt_id
        lease_token = run.attempts[-1].lease_token
        uow.commit()

    attempt_holder = session_factory()
    terminal_done = Event()
    run_locked = Event()
    terminal_errors: list[BaseException] = []
    probe_error = None
    terminal_finished_while_attempt_locked = False
    attempt_transaction_still_open = False
    try:
        attempt_holder.begin()
        held = attempt_holder.execute(
            update(RunAttempt)
            .where(RunAttempt.attempt_id == attempt_id)
            .values(heartbeat_at=RunAttempt.heartbeat_at)
        )
        assert held.rowcount == 1

        terminal = RunnerOutcome(
            attempt_status=AttemptStatus.PASSED,
            exit_code=0,
            started_at=claimed_at,
            finished_at=claimed_at + timedelta(seconds=1),
            gate_result=GateResult(True, (), {}),
        )

        def finalize() -> None:
            try:
                with SqlAlchemyUnitOfWork(session_factory) as uow:
                    uow.runs.record_terminal_aggregate(
                        run_id,
                        attempt_id,
                        lease_token,
                        terminal,
                        RunOutcome.PASSED,
                        (),
                        now=claimed_at + timedelta(seconds=2),
                        on_run_locked=run_locked.set,
                    )
                    uow.commit()
            except BaseException as error:
                terminal_errors.append(error)
            finally:
                terminal_done.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            final_write = executor.submit(finalize)
            assert run_locked.wait(timeout=10)
            terminal_finished_while_attempt_locked = terminal_done.wait(timeout=2)
            attempt_transaction_still_open = attempt_holder.in_transaction()
            try:
                with session_factory.begin() as session:
                    session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    locked_run = session.scalar(
                        select(Run).where(Run.run_id == run_id).with_for_update()
                    )
                    assert locked_run.run_id == run_id
            except BaseException as error:
                probe_error = error
            finally:
                attempt_holder.rollback()
                final_write.result(timeout=10)
    finally:
        if attempt_holder.in_transaction():
            attempt_holder.rollback()
        attempt_holder.close()

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)

        assert terminal_finished_while_attempt_locked is True
        assert attempt_transaction_still_open is True
        assert probe_error is None
        assert len(terminal_errors) == 1
        error = terminal_errors[0]
        assert type(error) is LeaseLostError
        assert str(error) == "terminal database lock acquisition timed out"
        assert isinstance(error.__cause__, OperationalError)
        assert getattr(error.__cause__.orig, "sqlstate", None) == "55P03"
        assert run.status is RunStatus.RUNNING
        assert attempt.status is AttemptStatus.RUNNING


class RichFailingRunner:
    def __init__(self, staging_root: Path) -> None:
        self._staging_root = staging_root
        self.call = None
        self.calls = 0

    def run(self, spec, workspace, heartbeat):
        self.calls += 1
        self.call = (spec, workspace)
        assert spec.allowed_workspace_root == workspace.resolve()
        assert spec.parameters == {"scenario": "stored"}
        assert spec.gate_policy.min_pass_rate == 1.0
        assert tuple(spec.argv) == ("python", "-m", "pytest", "suite.py")
        (workspace / "suite.py").write_text("attempt changed only\n", encoding="utf-8")
        self._staging_root.mkdir()
        artifact_path = self._staging_root / "junit.xml"
        artifact_path.write_bytes(b"<testsuite/>\n")
        started = datetime(2026, 8, 10, 2, 0, 1, tzinfo=UTC)
        return RunnerOutcome(
            attempt_status=AttemptStatus.TEST_FAILED,
            exit_code=1,
            started_at=started,
            finished_at=started + timedelta(seconds=2),
            case_results=(
                CaseResultData("suite.py::test_one", "failed", 12.5, "assert 1 == 2"),
            ),
            case_summary=CaseSummary(1, 0, 1, 0, 0),
            performance_summary=PerformanceSummary(
                request_count=4,
                p95_ms=250.0,
                failure_ratio=0.25,
                requests_per_second=2.0,
                average_response_time_ms=100.0,
                failure_count=1,
            ),
            gate_result=GateResult(
                False, ("failures",), {"pass_rate": 0.0, "failures": 1.0}
            ),
            artifacts=(
                RunnerArtifact(
                    "junit_xml", artifact_path, self._staging_root, "application/xml"
                ),
            ),
            failure_kind="test_failure",
            failure_summary="one assertion failed",
        )


class FailingArtifactStore:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def put(self, *_args, **_kwargs):
        raise self._error


class BlockingFailingArtifactStore:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.started = Event()
        self.release = Event()

    def put(self, *_args, **_kwargs):
        self.started.set()
        assert self.release.wait(timeout=10)
        raise self._error


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self._value = value
        self._lock = Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._value

    def set(self, value: datetime) -> None:
        with self._lock:
            self._value = value


class PartiallyPersistableArtifactsRunner:
    def __init__(self, staging_root: Path) -> None:
        self._staging_root = staging_root

    def run(self, spec, workspace, heartbeat):
        self._staging_root.mkdir(parents=True)
        first = self._staging_root / "first.txt"
        second = self._staging_root / "second.txt"
        first.write_bytes(b"1")
        second.write_bytes(b"22")
        finished_at = datetime(2026, 8, 10, 2, 0, 2, tzinfo=UTC)
        return RunnerOutcome(
            attempt_status=AttemptStatus.PASSED,
            exit_code=0,
            started_at=finished_at - timedelta(seconds=1),
            finished_at=finished_at,
            gate_result=GateResult(True, (), {"pass_rate": 1.0}),
            artifacts=(
                RunnerArtifact("first", first, self._staging_root, "text/plain"),
                RunnerArtifact("second", second, self._staging_root, "text/plain"),
            ),
        )


class CleanupFailingFileArtifactStore(FileArtifactStore):
    def discard(self, uri: str) -> None:
        raise OSError("sentinel cleanup path")


class MixedOwnershipArtifactRunner:
    def __init__(self, external_root: Path, service_child: Path) -> None:
        self._external_root = external_root
        self._service_child = service_child

    def run(self, spec, workspace, heartbeat):
        self._external_root.mkdir()
        self._service_child.mkdir(parents=True)
        external = self._external_root / "external.txt"
        owned = self._service_child / "owned.txt"
        external.write_text("preserve me", encoding="utf-8")
        owned.write_text("clean me", encoding="utf-8")
        now = datetime(2026, 8, 10, 2, 0, 2, tzinfo=UTC)
        return RunnerOutcome(
            attempt_status=AttemptStatus.PASSED,
            exit_code=0,
            started_at=now - timedelta(seconds=1),
            finished_at=now,
            gate_result=GateResult(True, (), {"pass_rate": 1.0}),
            artifacts=(
                RunnerArtifact(
                    "external",
                    external,
                    self._external_root,
                    "text/plain",
                ),
                RunnerArtifact(
                    "owned",
                    owned,
                    self._service_child,
                    "text/plain",
                ),
            ),
        )


def test_worker_only_cleans_strict_descendants_of_service_staging_root(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "staging-ownership")
    external_root = tmp_path / "custom-runner-output"
    staging_root = tmp_path / "service-staging"
    service_child = staging_root / "attempt-output"
    runner = MixedOwnershipArtifactRunner(external_root, service_child)
    worker = RunWorker(
        session_factory,
        runners={"pytest": runner},
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        workspace_root=tmp_path / "workspaces",
        staging_root=staging_root,
        clock=lambda: datetime(2026, 8, 10, 2, 0, 3, tzinfo=UTC),
    )

    assert worker.execute(run_id) is True
    assert (external_root / "external.txt").read_text(encoding="utf-8") == (
        "preserve me"
    )
    assert staging_root.is_dir()
    assert not service_child.exists()


def test_terminal_write_persists_entire_aggregate_from_immutable_snapshot(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "suite.py"
    source_file.write_text("trusted snapshot source\n", encoding="utf-8")
    run_id = _create_snapshot_run(session_factory, source, "terminal-aggregate")
    staging_root = tmp_path / "runner-staging"
    staging_root.mkdir()
    runner_staging = staging_root / "attempt-output"
    runner = RichFailingRunner(runner_staging)
    artifact_store = FileArtifactStore(tmp_path / "artifacts")
    worker = RunWorker(
        session_factory,
        runners={"pytest": runner},
        artifact_store=artifact_store,
        workspace_root=tmp_path / "workspaces",
        staging_root=staging_root,
        clock=lambda: datetime(2026, 8, 10, 2, 0, tzinfo=UTC),
    )

    assert worker.execute(run_id, worker_id="worker-rich") is True
    assert worker.execute(run_id, worker_id="worker-duplicate") is False

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(select(RunAttempt).where(RunAttempt.run_id == run_id))
        case = session.scalar(select(CaseResult).where(CaseResult.attempt_id == attempt.attempt_id))
        metrics = session.scalars(
            select(Metric).where(Metric.attempt_id == attempt.attempt_id)
        ).all()
        artifact = session.scalar(
            select(Artifact).where(Artifact.attempt_id == attempt.attempt_id)
        )
        gate = session.scalar(
            select(GateEvaluation).where(GateEvaluation.attempt_id == attempt.attempt_id)
        )
        finished_events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.event_type == "run.finished"
            )
        ).all()

        assert run.status is RunStatus.COMPLETED
        assert run.outcome is RunOutcome.FAILED
        assert attempt.status is AttemptStatus.TEST_FAILED
        assert attempt.exit_code == 1
        assert attempt.failure_reason == "one assertion failed"
        assert attempt.started_at == datetime(2026, 8, 10, 2, 0, 1, tzinfo=UTC)
        assert attempt.finished_at == datetime(2026, 8, 10, 2, 0, 3, tzinfo=UTC)
        assert (case.node_id, case.status, case.duration_ms, case.message) == (
            "suite.py::test_one",
            "failed",
            12.5,
            "assert 1 == 2",
        )
        assert {metric.metric_name for metric in metrics} == {
            "cases_total",
            "cases_passed",
            "cases_failed",
            "cases_errors",
            "cases_skipped",
            "request_count",
            "p95_ms",
            "failure_ratio",
            "requests_per_second",
            "average_response_time_ms",
            "failure_count",
        }
        assert gate.passed is False
        assert gate.reason_codes == ["failures"]
        assert artifact.checksum
        assert artifact.artifact_metadata["size_bytes"] == len("<testsuite/>\n")
        assert artifact_store.resolve(artifact.uri).read_text(encoding="utf-8") == (
            "<testsuite/>\n"
        )
        assert len(finished_events) == 1

    assert source_file.read_text(encoding="utf-8") == "trusted snapshot source\n"
    assert runner.call[1] != source
    assert runner.call[1].is_relative_to(tmp_path / "workspaces")
    assert not runner.call[1].exists()
    assert runner.calls == 1
    assert staging_root.is_dir()
    assert not runner_staging.exists()


@pytest.mark.parametrize(
    "artifact_error",
    [OSError("sentinel path"), ArtifactStoreError("sentinel path")],
    ids=["os-error", "artifact-store-error"],
)
def test_artifact_persistence_failure_is_terminal_and_never_schedules_retry(
    session_factory, tmp_path: Path, artifact_error: Exception
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "artifact-persistence-failure",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    staging_root = tmp_path / "runner-staging"
    staging_root.mkdir()
    runner_staging = staging_root / "attempt-output"
    worker = RunWorker(
        session_factory,
        runners={"pytest": RichFailingRunner(runner_staging)},
        artifact_store=FailingArtifactStore(artifact_error),
        workspace_root=tmp_path / "workspaces",
        staging_root=staging_root,
        clock=lambda: datetime(2026, 8, 10, 2, 0, 3, tzinfo=UTC),
    )

    execution_result = None
    try:
        execution_result = worker.execute(run_id)
    except (ArtifactStoreError, OSError) as error:
        assert str(error) == "sentinel path"

    with session_factory() as session:
        run = session.scalar(
            select(Run)
            .where(Run.run_id == run_id)
        )
        retry_event_count = len(
            session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "run.retry_scheduled",
                )
            ).all()
        )
        retry_outbox_count = len(
            session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == run_id,
                    OutboxEvent.event_type == "run.retry_scheduled",
                )
            ).all()
        )

        assert run.status is RunStatus.INFRA_FAILED
        assert run.outcome is RunOutcome.UNKNOWN
        assert run.attempts[0].status is AttemptStatus.INFRA_FAILED
        assert "sentinel path" not in (run.attempts[0].failure_reason or "")
        assert retry_event_count == 0
        assert retry_outbox_count == 0
    assert execution_result is True
    assert staging_root.is_dir()
    assert not runner_staging.exists()
    assert not (tmp_path / "workspaces" / str(run_id)).exists()


def test_blocked_artifact_failure_crossing_lease_stays_terminal_without_retry(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "blocked-artifact-failure",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    initial_time = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    clock = MutableClock(initial_time)
    staging_root = tmp_path / "runner-staging"
    staging_root.mkdir()
    runner_staging = staging_root / "attempt-output"
    artifact_store = BlockingFailingArtifactStore(
        ArtifactStoreError("sentinel delayed path")
    )
    worker = RunWorker(
        session_factory,
        runners={"pytest": RichFailingRunner(runner_staging)},
        artifact_store=artifact_store,
        workspace_root=tmp_path / "workspaces",
        staging_root=staging_root,
        lease_duration=timedelta(milliseconds=300),
        clock=clock,
    )

    execution_result = None
    execution_error = None
    heartbeat_extended = False
    with ThreadPoolExecutor(max_workers=1) as executor:
        execution = executor.submit(worker.execute, run_id)
        assert artifact_store.started.wait(timeout=10)
        heartbeat_time = initial_time + timedelta(milliseconds=200)
        clock.set(heartbeat_time)
        heartbeat_deadline = time.monotonic() + 2
        while time.monotonic() < heartbeat_deadline:
            with session_factory() as session:
                attempt = session.scalar(
                    select(RunAttempt).where(RunAttempt.run_id == run_id)
                )
                if (
                    attempt.heartbeat_at == heartbeat_time
                    and attempt.lease_expires_at
                    == heartbeat_time + timedelta(milliseconds=300)
                ):
                    heartbeat_extended = True
                    break
            time.sleep(0.01)

        reconciled_at = initial_time + timedelta(milliseconds=400)
        clock.set(reconciled_at)
        artifact_store.release.set()
        try:
            execution_result = execution.result(timeout=10)
        except BaseException as error:
            execution_error = error

    reconciled = LeaseReconciler(session_factory).reconcile_once(now=reconciled_at)

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempts = session.scalars(
            select(RunAttempt)
            .where(RunAttempt.run_id == run_id)
            .order_by(RunAttempt.attempt_no)
        ).all()
        retry_events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        retry_outbox = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "run.retry_scheduled",
            )
        ).all()

        assert run.status is RunStatus.INFRA_FAILED
        assert run.outcome is RunOutcome.UNKNOWN
        assert len(attempts) == 1
        assert attempts[0].status is AttemptStatus.INFRA_FAILED
        assert retry_events == []
        assert retry_outbox == []

    assert heartbeat_extended is True
    assert execution_error is None
    assert execution_result is True
    assert reconciled == 0
    assert not any(
        thread.name.startswith("quality-flow-lease-keeper-")
        for thread in enumerate_threads()
    )
    assert staging_root.is_dir()
    assert not runner_staging.exists()
    assert not (tmp_path / "workspaces" / str(run_id)).exists()


def test_later_artifact_failure_rolls_back_files_already_stored_in_batch(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "artifact-batch-rollback",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    artifact_root = tmp_path / "artifacts"
    staging_root = tmp_path / "runner-staging"
    runner_staging = staging_root / "attempt-output"
    worker = RunWorker(
        session_factory,
        runners={"pytest": PartiallyPersistableArtifactsRunner(runner_staging)},
        artifact_store=FileArtifactStore(artifact_root, max_file_bytes=1),
        workspace_root=tmp_path / "workspaces",
        staging_root=staging_root,
        clock=lambda: datetime(2026, 8, 10, 2, 0, 3, tzinfo=UTC),
    )

    assert worker.execute(run_id) is True

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(
            select(RunAttempt).where(RunAttempt.run_id == run_id)
        )
        artifacts = session.scalars(
            select(Artifact).where(Artifact.attempt_id == attempt.attempt_id)
        ).all()
        retry_events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        retry_outbox = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "run.retry_scheduled",
            )
        ).all()

        assert run.status is RunStatus.INFRA_FAILED
        assert run.outcome is RunOutcome.UNKNOWN
        assert attempt.status is AttemptStatus.INFRA_FAILED
        assert artifacts == []
        assert retry_events == []
        assert retry_outbox == []

    permanent_files = [
        path for path in artifact_root.rglob("*") if path.is_file()
    ]
    assert permanent_files == []


def test_artifact_rollback_cleanup_error_is_safely_logged_and_still_terminal(
    session_factory, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "artifact-rollback-cleanup-error",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    staging_root = tmp_path / "runner-staging"
    worker = RunWorker(
        session_factory,
        runners={
            "pytest": PartiallyPersistableArtifactsRunner(
                staging_root / "attempt-output"
            )
        },
        artifact_store=CleanupFailingFileArtifactStore(
            tmp_path / "artifacts", max_file_bytes=1
        ),
        workspace_root=tmp_path / "workspaces",
        staging_root=staging_root,
        clock=lambda: datetime(2026, 8, 10, 2, 0, 3, tzinfo=UTC),
    )

    with caplog.at_level(logging.WARNING, logger="quality_flow.application.worker"):
        assert worker.execute(run_id) is True

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(
            select(RunAttempt).where(RunAttempt.run_id == run_id)
        )
        assert run.status is RunStatus.INFRA_FAILED
        assert attempt.status is AttemptStatus.INFRA_FAILED
    assert [record.getMessage() for record in caplog.records] == [
        "artifact rollback failed"
    ]
    assert "sentinel cleanup path" not in caplog.text


def test_real_terminal_commit_failure_rolls_back_entire_aggregate(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "terminal-rollback")
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(run_id, now=now)
        attempt_id = run.attempts[-1].attempt_id
        token = run.attempts[-1].lease_token
        uow.commit()

    terminal = RunnerOutcome(
        attempt_status=AttemptStatus.TEST_FAILED,
        exit_code=1,
        started_at=now,
        finished_at=now + timedelta(seconds=1),
        case_results=(CaseResultData("suite.py::test_one", "failed", 1.0),),
        case_summary=CaseSummary(1, 0, 1, 0, 0),
        gate_result=GateResult(False, ("failures",), {"failures": 1.0}),
        failure_summary="expected failure",
    )
    staging = tmp_path / "rollback-staging"
    staging.mkdir()
    artifact_source = staging / "result.txt"
    artifact_source.write_bytes(b"copied before transaction")
    stored_artifact = FileArtifactStore(tmp_path / "rollback-artifacts").put(
        artifact_source,
        ArtifactMetadata(run_id, attempt_id, "stdout", "text/plain"),
        attempt_workspace=staging,
    )

    with pytest.raises(IntegrityError):
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.record_terminal_aggregate(
                run_id,
                attempt_id,
                token,
                terminal,
                RunOutcome.FAILED,
                (stored_artifact,),
                now=now + timedelta(seconds=2),
            )
            uow.session.add(
                CaseResult(
                    attempt_id=attempt_id,
                    node_id=None,
                    status="failed",
                    duration_ms=1.0,
                    details={},
                    created_at=now,
                )
            )
            uow.commit()

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        assert run.status is RunStatus.RUNNING
        assert run.outcome is RunOutcome.UNKNOWN
        assert attempt.status is AttemptStatus.RUNNING
        assert session.scalars(
            select(CaseResult).where(CaseResult.attempt_id == attempt_id)
        ).all() == []
        assert session.scalars(
            select(Metric).where(Metric.attempt_id == attempt_id)
        ).all() == []
        assert session.scalars(
            select(GateEvaluation).where(GateEvaluation.attempt_id == attempt_id)
        ).all() == []
        assert session.scalars(
            select(Artifact).where(Artifact.attempt_id == attempt_id)
        ).all() == []
        assert session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.event_type == "run.finished"
            )
        ).all() == []


def test_reconciler_requeues_first_expired_attempt_and_appends_retry_events(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "first-expiry-retry",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=10)
        )
        attempt_id = run.attempts[-1].attempt_id
        reconcile_at = run.attempts[-1].lease_expires_at
        uow.commit()

    assert LeaseReconciler(session_factory).reconcile_once(now=reconcile_at) == 1

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        retries = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        retry_outbox = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        assert run.status is RunStatus.QUEUED
        assert run.outcome is RunOutcome.UNKNOWN
        assert run.finished_at is None
        assert run.started_at == claimed_at
        assert attempt.status is AttemptStatus.ABANDONED
        assert attempt.finished_at == reconcile_at
        assert attempt.failure_reason == "worker lease expired"
        assert len(retries) == len(retry_outbox) == 1
        assert retries[0].payload == {
            "status": RunStatus.QUEUED.value,
            "outcome": RunOutcome.UNKNOWN.value,
            "attempt_no": 1,
            "next_attempt_no": 2,
            "reason": "worker_lost",
        }
        assert retry_outbox[0].payload == {"run_id": str(run_id)}
        assert retry_outbox[0].published_at is None
        assert retry_outbox[0].publish_attempts == 0


def test_retry_claim_preserves_started_at_and_second_expiry_exhausts_budget(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "retry-budget",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    first_claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=first_claimed_at, lease_duration=timedelta(seconds=10)
        )
        first_expiry = run.attempts[-1].lease_expires_at
        uow.commit()

    assert LeaseReconciler(session_factory).reconcile_once(now=first_expiry) == 1

    second_claimed_at = first_expiry + timedelta(seconds=1)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=second_claimed_at, lease_duration=timedelta(seconds=10)
        )
        assert run is not None
        assert run.started_at == first_claimed_at
        assert run.attempts[-1].attempt_no == 2
        second_attempt_id = run.attempts[-1].attempt_id
        second_expiry = run.attempts[-1].lease_expires_at
        uow.commit()

    assert LeaseReconciler(session_factory).reconcile_once(now=second_expiry) == 1

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempts = session.scalars(
            select(RunAttempt)
            .where(RunAttempt.run_id == run_id)
            .order_by(RunAttempt.attempt_no)
        ).all()
        retries = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        abandonments = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.abandoned",
            )
        ).all()
        retry_outbox = session.scalars(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == run_id)
        ).all()
        assert run.status is RunStatus.INFRA_FAILED
        assert run.outcome is RunOutcome.UNKNOWN
        assert run.finished_at == second_expiry
        assert run.started_at == first_claimed_at
        assert [attempt.attempt_no for attempt in attempts] == [1, 2]
        assert [attempt.status for attempt in attempts] == [
            AttemptStatus.ABANDONED,
            AttemptStatus.ABANDONED,
        ]
        assert attempts[-1].attempt_id == second_attempt_id
        assert attempts[-1].failure_reason == "worker lease expired"
        assert len(retries) == len(abandonments) == len(retry_outbox) == 1
        assert retry_outbox[0].event_type == "run.retry_scheduled"


def test_two_reconcilers_schedule_exactly_one_retry_event_and_outbox(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "two-reconciler-retry",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=5)
        )
        reconcile_at = run.attempts[-1].lease_expires_at
        uow.commit()

    barrier = Barrier(2)

    def reconcile() -> int:
        barrier.wait(timeout=10)
        return LeaseReconciler(session_factory).reconcile_once(now=reconcile_at)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(reconcile) for _ in range(2)]
        assert sum(result.result(timeout=10) for result in results) == 1

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempts = session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == run_id)
        ).all()
        retries = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        retry_outbox = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        assert run.status is RunStatus.QUEUED
        assert attempts[0].status is AttemptStatus.ABANDONED
        assert len(attempts) == len(retries) == len(retry_outbox) == 1


def test_retry_commit_failure_rolls_back_run_attempt_and_events(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "retry-rollback",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=5)
        )
        attempt_id = run.attempts[-1].attempt_id
        reconcile_at = run.attempts[-1].lease_expires_at
        uow.commit()

    class FailingRetryFlushSession(Session):
        def flush(self, objects=None) -> None:
            fail_after_flush = any(
                isinstance(row, OutboxEvent)
                and row.event_type == "run.retry_scheduled"
                for row in self.new
            )
            super().flush(objects)
            if fail_after_flush:
                raise RuntimeError("injected retry commit failure")

    failing_factory = sessionmaker(
        bind=session_factory.kw["bind"],
        class_=FailingRetryFlushSession,
        expire_on_commit=False,
    )
    with pytest.raises(RuntimeError, match="injected retry commit failure"):
        LeaseReconciler(failing_factory).reconcile_once(now=reconcile_at)

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        retry_events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        retry_outbox = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        assert run.status is RunStatus.RUNNING
        assert run.outcome is RunOutcome.UNKNOWN
        assert run.finished_at is None
        assert attempt.status is AttemptStatus.RUNNING
        assert attempt.finished_at is None
        assert attempt.failure_reason is None
        assert retry_events == []
        assert retry_outbox == []


def test_reconciler_abandons_expired_lease_and_fences_old_worker(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "expired-reconcile")
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=10)
        )
        attempt_id = run.attempts[-1].attempt_id
        token = run.attempts[-1].lease_token
        uow.commit()

    reconciled_at = claimed_at + timedelta(seconds=11)
    reconciler = LeaseReconciler(session_factory)
    assert reconciler.reconcile_once(now=reconciled_at) == 1

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.abandoned",
            )
        ).all()
        assert run.status is RunStatus.INFRA_FAILED
        assert run.outcome is RunOutcome.UNKNOWN
        assert run.finished_at == reconciled_at
        assert attempt.status is AttemptStatus.ABANDONED
        assert attempt.finished_at == reconciled_at
        assert len(events) == 1

    with pytest.raises(LeaseLostError):
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.heartbeat(
                attempt_id,
                token,
                now=reconciled_at,
                lease_duration=timedelta(seconds=10),
            )

    stale_outcome = RunnerOutcome(
        attempt_status=AttemptStatus.PASSED,
        exit_code=0,
        started_at=claimed_at,
        finished_at=reconciled_at,
        gate_result=GateResult(True, (), {}),
    )
    with pytest.raises(LeaseLostError):
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.runs.record_terminal_aggregate(
                run_id,
                attempt_id,
                token,
                stale_outcome,
                RunOutcome.PASSED,
                (),
                now=reconciled_at,
            )


def test_reconciler_rejects_candidate_when_lease_token_changes_before_lock(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "reconcile-token-race")
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        claimed = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=5)
        )
        attempt_id = claimed.attempts[-1].attempt_id
        old_token = claimed.attempts[-1].lease_token
        uow.commit()

    before_run_lock = Event()
    release_run_lock = Event()

    class PausingSession(Session):
        def scalar(self, statement, *args, **kwargs):
            rendered = str(statement)
            if "FROM runs" in rendered and "FOR UPDATE" in rendered:
                before_run_lock.set()
                assert release_run_lock.wait(timeout=10)
            return super().scalar(statement, *args, **kwargs)

    pausing_factory = sessionmaker(
        bind=session_factory.kw["bind"],
        class_=PausingSession,
        expire_on_commit=False,
    )
    reconcile_at = claimed_at + timedelta(seconds=6)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            LeaseReconciler(pausing_factory).reconcile_once, now=reconcile_at
        )
        assert before_run_lock.wait(timeout=10)
        new_token = uuid4()
        assert new_token != old_token
        with session_factory.begin() as session:
            session.execute(
                update(RunAttempt)
                .where(RunAttempt.attempt_id == attempt_id)
                .values(lease_token=new_token)
            )
        release_run_lock.set()
        assert future.result(timeout=10) == 0

    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        assert (run.status, attempt.status, attempt.lease_token) == (
            RunStatus.RUNNING,
            AttemptStatus.RUNNING,
            new_token,
        )
        assert session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.abandoned",
            )
        ).all() == []


def test_two_reconcilers_append_exactly_one_abandonment_event(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "two-reconcilers")
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=5)
        )
        uow.commit()

    barrier = Barrier(2)

    def reconcile() -> int:
        barrier.wait(timeout=10)
        return LeaseReconciler(session_factory).reconcile_once(
            now=claimed_at + timedelta(seconds=6)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(reconcile) for _ in range(2)]
        assert sum(result.result(timeout=10) for result in results) == 1

    with session_factory() as session:
        events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.abandoned",
            )
        ).all()
        attempts = session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == run_id)
        ).all()
        assert len(events) == 1
        assert len(attempts) == 1


def test_terminal_vs_reconcile_race_never_creates_mixed_state(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(session_factory, source, "terminal-reconcile-race")
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=10)
        )
        attempt_id = run.attempts[-1].attempt_id
        token = run.attempts[-1].lease_token
        uow.commit()

    terminal = RunnerOutcome(
        attempt_status=AttemptStatus.PASSED,
        exit_code=0,
        started_at=claimed_at,
        finished_at=claimed_at + timedelta(seconds=9),
        gate_result=GateResult(True, (), {}),
    )
    barrier = Barrier(2)

    def finalize() -> str:
        barrier.wait(timeout=10)
        try:
            with SqlAlchemyUnitOfWork(session_factory) as uow:
                uow.runs.record_terminal_aggregate(
                    run_id,
                    attempt_id,
                    token,
                    terminal,
                    RunOutcome.PASSED,
                    (),
                    now=claimed_at + timedelta(seconds=9),
                )
                uow.commit()
            return "finished"
        except LeaseLostError:
            return "lost"

    def reconcile() -> int:
        barrier.wait(timeout=10)
        return LeaseReconciler(session_factory).reconcile_once(
            now=claimed_at + timedelta(seconds=11)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        final_future = executor.submit(finalize)
        reconcile_future = executor.submit(reconcile)
        result = (final_future.result(timeout=10), reconcile_future.result(timeout=10))

    assert result in {("finished", 0), ("lost", 1)}
    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        event_types = session.scalars(
            select(RunEvent.event_type).where(RunEvent.run_id == run_id)
        ).all()
        if result == ("finished", 0):
            assert (run.status, run.outcome, attempt.status) == (
                RunStatus.COMPLETED,
                RunOutcome.PASSED,
                AttemptStatus.PASSED,
            )
            assert event_types.count("run.finished") == 1
            assert "run.abandoned" not in event_types
        else:
            assert (run.status, run.outcome, attempt.status) == (
                RunStatus.INFRA_FAILED,
                RunOutcome.UNKNOWN,
                AttemptStatus.ABANDONED,
            )
            assert event_types.count("run.abandoned") == 1
            assert "run.finished" not in event_types


def test_retry_enabled_terminal_vs_reconcile_race_never_creates_mixed_state(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_id = _create_snapshot_run(
        session_factory,
        source,
        "retry-terminal-reconcile-race",
        retry_policy={"max_attempts": 2, "retry_on": ["worker_lost"]},
    )
    claimed_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = uow.runs.claim_queued_run(
            run_id, now=claimed_at, lease_duration=timedelta(seconds=10)
        )
        attempt_id = run.attempts[-1].attempt_id
        token = run.attempts[-1].lease_token
        uow.commit()

    terminal = RunnerOutcome(
        attempt_status=AttemptStatus.PASSED,
        exit_code=0,
        started_at=claimed_at,
        finished_at=claimed_at + timedelta(seconds=9),
        gate_result=GateResult(True, (), {}),
    )
    barrier = Barrier(2)

    def finalize() -> str:
        barrier.wait(timeout=10)
        try:
            with SqlAlchemyUnitOfWork(session_factory) as uow:
                uow.runs.record_terminal_aggregate(
                    run_id,
                    attempt_id,
                    token,
                    terminal,
                    RunOutcome.PASSED,
                    (),
                    now=claimed_at + timedelta(seconds=9),
                )
                uow.commit()
            return "finished"
        except LeaseLostError:
            return "lost"

    def reconcile() -> int:
        barrier.wait(timeout=10)
        return LeaseReconciler(session_factory).reconcile_once(
            now=claimed_at + timedelta(seconds=11)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        final_future = executor.submit(finalize)
        reconcile_future = executor.submit(reconcile)
        result = (final_future.result(timeout=10), reconcile_future.result(timeout=10))

    assert result in {("finished", 0), ("lost", 1)}
    with session_factory() as session:
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        event_types = session.scalars(
            select(RunEvent.event_type).where(RunEvent.run_id == run_id)
        ).all()
        retry_events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        retry_outbox = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "run.retry_scheduled",
            )
        ).all()
        if result == ("finished", 0):
            assert (run.status, run.outcome, attempt.status) == (
                RunStatus.COMPLETED,
                RunOutcome.PASSED,
                AttemptStatus.PASSED,
            )
            assert event_types.count("run.finished") == 1
            assert "run.abandoned" not in event_types
            assert retry_events == []
            assert retry_outbox == []
        else:
            assert (run.status, run.outcome, attempt.status) == (
                RunStatus.QUEUED,
                RunOutcome.UNKNOWN,
                AttemptStatus.ABANDONED,
            )
            assert run.finished_at is None
            assert attempt.failure_reason == "worker lease expired"
            assert "run.finished" not in event_types
            assert "run.abandoned" not in event_types
            assert event_types.count("run.retry_scheduled") == 1
            assert len(retry_events) == len(retry_outbox) == 1
            assert retry_outbox[0].published_at is None


def test_real_redis_transport_uses_isolated_database_and_unique_queue() -> None:
    redis_url = os.environ.get("TASK7_REDIS_URL", "redis://127.0.0.1:6379/14")
    queue_name = f"qf-task7-{uuid4().hex}"
    binding_key = f"_kombu.binding.{queue_name}"
    client = Redis.from_url(redis_url, decode_responses=False)
    app = create_celery_app(redis_url, queue_name=queue_name)
    event_id = uuid4()
    run_id = uuid4()
    try:
        assert client.ping() is True
        assert client.exists(queue_name) == 0

        CeleryRunPublisher(app, queue_name=queue_name).publish(
            event_id=event_id, run_id=run_id
        )

        raw_message = client.lpop(queue_name)
        assert raw_message is not None
        envelope = json.loads(raw_message)
        body = json.loads(base64.b64decode(envelope["body"]))
        assert body[0] == []
        assert body[1] == {"event_id": str(event_id), "run_id": str(run_id)}
        assert envelope["headers"]["id"] == str(event_id)
        assert envelope["headers"]["task"] == "quality_flow.execute_run"
    finally:
        app.close()
        client.delete(queue_name, binding_key)
        client.close()


def test_schema_enforces_running_lease_completeness_and_stale_lookup_index(
    session_factory,
) -> None:
    inspector = inspect(session_factory.kw["bind"])
    checks = {
        check["name"]: check["sqltext"]
        for check in inspector.get_check_constraints("run_attempts")
    }
    indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspector.get_indexes("run_attempts")
    }

    assert "ck_run_attempts_running_lease" in checks
    assert "lease_token" in checks["ck_run_attempts_running_lease"]
    assert "heartbeat_at" in checks["ck_run_attempts_running_lease"]
    assert "lease_expires_at" in checks["ck_run_attempts_running_lease"]
    assert indexes["ix_run_attempts_status_lease_expires_at"] == (
        "status",
        "lease_expires_at",
    )


def test_attempt_lease_migration_is_reversible_in_isolated_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_name = f"quality_flow_task7_{uuid4().hex}"
    admin_url = make_url(
        os.environ.get(
            "QUALITY_FLOW_TEST_ADMIN_DATABASE_URL", DEFAULT_ADMIN_DATABASE_URL
        )
    )
    if admin_url.get_backend_name() != "postgresql":
        raise ValueError("test admin database must use PostgreSQL")
    connection_parameters = {
        "dbname": admin_url.database,
        "host": admin_url.host,
        "port": admin_url.port,
        "user": admin_url.username,
        "password": admin_url.password,
    }
    connection_parameters = {
        key: value for key, value in connection_parameters.items() if value is not None
    }
    isolated_url = admin_url.set(
        drivername="postgresql+psycopg", database=database_name
    ).render_as_string(hide_password=False)
    with psycopg.connect(**connection_parameters, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )

    engine = create_engine(isolated_url)
    try:
        monkeypatch.setenv("DATABASE_URL", isolated_url)
        config = Config(str(project_root / "alembic.ini"))
        command.upgrade(config, "0001_initial_schema")
        command.upgrade(config, "head")
        assert "ck_run_attempts_running_lease" in {
            check["name"]
            for check in inspect(engine).get_check_constraints("run_attempts")
        }
        assert "ix_run_attempts_status_lease_expires_at" in {
            index["name"] for index in inspect(engine).get_indexes("run_attempts")
        }

        command.downgrade(config, "0001_initial_schema")
        assert "ck_run_attempts_running_lease" not in {
            check["name"]
            for check in inspect(engine).get_check_constraints("run_attempts")
        }
        assert "ix_run_attempts_status_lease_expires_at" not in {
            index["name"] for index in inspect(engine).get_indexes("run_attempts")
        }

        command.upgrade(config, "head")
        assert "ck_run_attempts_running_lease" in {
            check["name"]
            for check in inspect(engine).get_check_constraints("run_attempts")
        }
        assert "ix_run_attempts_status_lease_expires_at" in {
            index["name"] for index in inspect(engine).get_indexes("run_attempts")
        }
    finally:
        engine.dispose()
        with psycopg.connect(**connection_parameters, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
            )
