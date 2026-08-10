from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql
from redis import Redis
from sqlalchemy import create_engine, delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from quality_flow.application.dispatcher import OutboxDispatcher
from quality_flow.application.reconciler import LeaseReconciler
from quality_flow.application.run_service import RunService
from quality_flow.application.worker import RunWorker
from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
from quality_flow.infrastructure.artifacts import ArtifactMetadata, FileArtifactStore
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


def _create_snapshot_run(session_factory, source: Path, key: str):
    run_id = uuid4()
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        session.add(
            Run(
                run_id=run_id,
                suite_id="snapshot-suite",
                idempotency_key=key,
                parameters={"scenario": "stored"},
                suite_snapshot={
                    "suite_id": "snapshot-suite",
                    "runner_type": "pytest",
                    "working_directory": str(source),
                    "argv": ["python", "-m", "pytest", "suite.py"],
                    "timeout_seconds": 5,
                    "allowed_parameters": {"scenario": ["stored"]},
                    "source_revision": "immutable-revision",
                },
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
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.calls = []

    def run(self, spec, workspace, heartbeat):
        self.calls.append((spec, workspace, heartbeat))
        self.started.set()
        assert self.release.wait(timeout=10)
        now = datetime(2026, 8, 10, 2, 0, 2, tzinfo=UTC)
        return RunnerOutcome(
            attempt_status=AttemptStatus.PASSED,
            exit_code=0,
            started_at=now - timedelta(seconds=1),
            finished_at=now,
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
    admin_url = "postgresql://quality_flow@127.0.0.1:55432/postgres"
    isolated_url = (
        f"postgresql+psycopg://quality_flow@127.0.0.1:55432/{database_name}"
    )
    with psycopg.connect(admin_url, autocommit=True) as connection:
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
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
            )
