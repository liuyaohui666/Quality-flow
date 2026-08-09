"""SQLAlchemy engine, session, and unit-of-work construction."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from quality_flow.application.run_service import NewOutboxEvent, NewRun, NewRunEvent
from quality_flow.infrastructure.models import OutboxEvent, Run, RunEvent
from quality_flow.infrastructure.repositories import RunRepository


SessionFactory = sessionmaker[Session]


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    return create_engine(database_url, echo=echo, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(bind=engine, expire_on_commit=False)


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self.session = self._session_factory()
        self.runs = RunRepository(self.session)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is not None:
                self.session.rollback()
        finally:
            self.session.close()

    def add_run(self, run: NewRun) -> None:
        self.runs.add(
            Run(
                run_id=run.run_id,
                suite_id=run.suite_id,
                idempotency_key=run.idempotency_key,
                parameters=run.parameters,
                suite_snapshot=run.suite_snapshot,
                gate_policy_snapshot=run.gate_policy_snapshot,
                status=run.status,
                created_at=run.created_at,
                updated_at=run.created_at,
            )
        )

    def add_run_event(self, event: NewRunEvent) -> None:
        self.session.add(
            RunEvent(
                event_id=event.event_id,
                run_id=event.run_id,
                event_type=event.event_type,
                payload=event.payload,
                created_at=event.created_at,
            )
        )

    def add_outbox_event(self, event: NewOutboxEvent) -> None:
        self.session.add(
            OutboxEvent(
                outbox_event_id=event.outbox_event_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                event_type=event.event_type,
                payload=event.payload,
                created_at=event.created_at,
            )
        )

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
