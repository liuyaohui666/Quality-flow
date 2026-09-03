"""Transactional creation of validated test runs."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from quality_flow.domain.enums import RunOutcome, RunStatus
from quality_flow.suites.registry import SuiteRegistry


@dataclass(frozen=True)
class NewRun:
    run_id: UUID
    suite_id: str
    idempotency_key: str
    parameters: dict[str, str]
    suite_snapshot: dict[str, Any]
    gate_policy_snapshot: dict[str, Any]
    status: RunStatus
    outcome: RunOutcome
    version: int
    created_at: datetime
    request_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class NewRunEvent:
    event_id: UUID
    run_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class NewOutboxEvent:
    outbox_event_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class RunReader(Protocol):
    def get_by_idempotency_key(self, idempotency_key: str) -> Any | None: ...


class RunUnitOfWork(Protocol):
    runs: RunReader

    def __enter__(self) -> RunUnitOfWork: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def add_run(self, run: NewRun) -> None: ...

    def add_run_event(self, event: NewRunEvent) -> None: ...

    def add_outbox_event(self, event: NewOutboxEvent) -> None: ...

    def commit(self) -> None: ...


RunUnitOfWorkFactory = Callable[[], RunUnitOfWork]


class DuplicateIdempotencyKeyError(RuntimeError):
    """Signal that another transaction won an idempotency-key race."""


class RunService:
    """Create validated runs behind an idempotent transactional boundary."""

    def __init__(
        self,
        uow_factory: RunUnitOfWorkFactory,
        registry: SuiteRegistry,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry

    def create_run(
        self,
        suite_id: str,
        idempotency_key: str,
        parameters: dict[str, str],
        request_body: dict[str, Any] | None = None,
    ) -> Any:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")

        suite = self._registry.get(suite_id)
        resolved_parameters = suite.resolve_parameters(parameters)
        resolved_request_body = suite.resolve_request_body(request_body)
        created_at = datetime.now(UTC)

        with self._uow_factory() as uow:
            existing = uow.runs.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing

            run_id = uuid4()
            run = NewRun(
                run_id=run_id,
                suite_id=suite.suite_id,
                idempotency_key=idempotency_key,
                parameters=resolved_parameters,
                request_body=resolved_request_body,
                suite_snapshot={
                    "suite_id": suite.suite_id,
                    "runner_type": suite.runner_type,
                    "working_directory": str(suite.working_directory),
                    "argv": list(suite.argv),
                    "timeout_seconds": suite.timeout_seconds,
                    "allowed_parameters": {
                        name: list(values)
                        for name, values in suite.allowed_parameters.items()
                    },
                    "test_type": suite.test_type,
                    "request_body": (
                        {
                            "required": suite.request_body.required,
                            "schema": deepcopy(dict(suite.request_body.schema)),
                        }
                        if suite.request_body is not None
                        else None
                    ),
                    "source_revision": suite.source_revision,
                    "retry_policy": {
                        "max_attempts": suite.retry_policy.max_attempts,
                        "retry_on": sorted(suite.retry_policy.retry_on),
                    },
                },
                gate_policy_snapshot=asdict(suite.gate_policy),
                status=RunStatus.QUEUED,
                outcome=RunOutcome.UNKNOWN,
                version=1,
                created_at=created_at,
            )
            payload = {"run_id": str(run_id), "suite_id": suite.suite_id}
            uow.add_run(run)
            uow.add_run_event(
                NewRunEvent(
                    event_id=uuid4(),
                    run_id=run_id,
                    event_type="run.queued",
                    payload={"status": RunStatus.QUEUED.value},
                    created_at=created_at,
                )
            )
            uow.add_outbox_event(
                NewOutboxEvent(
                    outbox_event_id=uuid4(),
                    aggregate_type="run",
                    aggregate_id=run_id,
                    event_type="run.queued",
                    payload=payload,
                    created_at=created_at,
                )
            )
            try:
                uow.commit()
            except DuplicateIdempotencyKeyError:
                existing = uow.runs.get_by_idempotency_key(idempotency_key)
                if existing is None:
                    raise RuntimeError(
                        "Idempotency conflict was reported but the winning Run "
                        "could not be read"
                    )
                return existing
            return run
