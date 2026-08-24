from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from quality_flow.application.run_service import RunService
from quality_flow.domain.enums import RunStatus
from quality_flow.suites.registry import SuiteRegistry


@dataclass
class FakeRun:
    run_id: UUID
    suite_id: str
    idempotency_key: str
    parameters: dict[str, str]
    suite_snapshot: dict[str, Any]
    gate_policy_snapshot: dict[str, Any]
    status: RunStatus


@dataclass
class FakePersistence:
    created_runs: list[FakeRun] = field(default_factory=list)
    run_events: list[Any] = field(default_factory=list)
    outbox_events: list[Any] = field(default_factory=list)
    commits: int = 0
    rollbacks: int = 0


class FakeRunRepository:
    def __init__(self, runs: list[FakeRun]) -> None:
        self._runs = runs

    def get_by_idempotency_key(self, idempotency_key: str) -> FakeRun | None:
        return next(
            (run for run in self._runs if run.idempotency_key == idempotency_key),
            None,
        )

    def add(self, run: FakeRun) -> None:
        self._runs.append(run)


class FakeUnitOfWork:
    def __init__(self, persistence: FakePersistence | None = None) -> None:
        self.persistence = persistence or FakePersistence()
        self.created_runs = self.persistence.created_runs
        self.run_events = self.persistence.run_events
        self.outbox_events = self.persistence.outbox_events
        self.runs = FakeRunRepository(self.created_runs)
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None:
            self.rollback()

    def add_run(self, run: FakeRun) -> None:
        self.runs.add(run)

    def add_run_event(self, event: Any) -> None:
        self.run_events.append(event)

    def add_outbox_event(self, event: Any) -> None:
        self.outbox_events.append(event)

    def commit(self) -> None:
        self.commits += 1
        self.persistence.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        self.persistence.rollbacks += 1


class FakeUnitOfWorkFactory:
    def __init__(self) -> None:
        self.persistence = FakePersistence()
        self.instances: list[FakeUnitOfWork] = []

    def __call__(self) -> FakeUnitOfWork:
        uow = FakeUnitOfWork(self.persistence)
        self.instances.append(uow)
        return uow


@pytest.fixture
def registry() -> SuiteRegistry:
    project_root = Path(__file__).resolve().parents[2]
    return SuiteRegistry.from_yaml(project_root / "config" / "suites.yaml", project_root)


@pytest.fixture
def fake_uow_factory() -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory()


def test_duplicate_idempotency_key_returns_existing_run(
    fake_uow_factory: FakeUnitOfWorkFactory, registry: SuiteRegistry
) -> None:
    service = RunService(fake_uow_factory, registry)

    first = service.create_run("demo-api", "same-key", {"scenario": "ok"})
    second = service.create_run("demo-api", "same-key", {"scenario": "ok"})

    assert first.run_id == second.run_id
    assert len(fake_uow_factory.instances) == 2
    assert fake_uow_factory.instances[0] is not fake_uow_factory.instances[1]
    assert len(fake_uow_factory.persistence.created_runs) == 1
    assert len(fake_uow_factory.persistence.run_events) == 1
    assert len(fake_uow_factory.persistence.outbox_events) == 1
    assert fake_uow_factory.persistence.commits == 1


def test_create_run_stores_resolved_suite_and_gate_policy_snapshots(
    fake_uow_factory: FakeUnitOfWorkFactory, registry: SuiteRegistry
) -> None:
    run = RunService(fake_uow_factory, registry).create_run(
        "demo-api", "new-key", {"scenario": "error"}
    )

    assert run.status is RunStatus.QUEUED
    assert run.parameters == {"scenario": "error"}
    assert run.suite_snapshot == {
        "suite_id": "demo-api",
        "runner_type": "pytest",
        "working_directory": str(Path(__file__).resolve().parents[2]),
        "argv": [
            "python",
            "-m",
            "pytest",
            "demo_suites/api/test_target.py",
            "-q",
        ],
        "timeout_seconds": 3,
        "allowed_parameters": {"scenario": ["ok", "error", "slow"]},
        "source_revision": "main",
        "retry_policy": {
            "max_attempts": 2,
            "retry_on": ["worker_lost"],
        },
    }
    assert run.gate_policy_snapshot == {
        "min_pass_rate": 1.0,
        "max_failures": 0,
        "max_error_rate": None,
        "max_p95_ms": None,
        "min_requests": None,
    }
    assert fake_uow_factory.persistence.run_events[0].payload == {"status": "queued"}
    assert fake_uow_factory.persistence.outbox_events[0].event_type == "run.queued"


def test_invalid_parameter_does_not_start_a_transaction(
    fake_uow_factory: FakeUnitOfWorkFactory, registry: SuiteRegistry
) -> None:
    service = RunService(fake_uow_factory, registry)

    with pytest.raises(ValueError):
        service.create_run("demo-api", "bad-key", {"scenario": "not-allowed"})

    assert fake_uow_factory.instances == []
    assert fake_uow_factory.persistence.created_runs == []
    assert fake_uow_factory.persistence.run_events == []
    assert fake_uow_factory.persistence.outbox_events == []
    assert fake_uow_factory.persistence.commits == 0


def test_service_requests_a_fresh_uow_for_each_create(
    registry: SuiteRegistry,
) -> None:
    first_uow = FakeUnitOfWork()
    second_uow = FakeUnitOfWork()
    available = iter((first_uow, second_uow))

    service = RunService(lambda: next(available), registry)
    service.create_run("demo-api", "factory-first", {"scenario": "ok"})
    service.create_run("demo-api", "factory-second", {"scenario": "ok"})

    assert first_uow.commits == 1
    assert second_uow.commits == 1
    assert len(first_uow.created_runs) == 1
    assert len(second_uow.created_runs) == 1
