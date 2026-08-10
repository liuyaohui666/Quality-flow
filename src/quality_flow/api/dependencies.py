"""Dependency construction and read-only queries for the HTTP API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from redis import Redis
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from quality_flow.application.run_service import RunService
from quality_flow.infrastructure.config import Settings
from quality_flow.infrastructure.database import (
    SessionFactory,
    SqlAlchemyUnitOfWork,
    make_engine,
    make_session_factory,
)
from quality_flow.infrastructure.models import (
    Artifact,
    CaseResult,
    GateEvaluation,
    Metric,
    Run,
)
from quality_flow.suites.registry import SuiteRegistry


class RunReader(Protocol):
    def get_run(self, run_id: UUID) -> Any | None: ...


class SqlAlchemyRunReader:
    """Load public run data without assigning or changing execution state."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def get_run(self, run_id: UUID) -> Run | None:
        with self._session_factory() as session:
            run = session.scalar(
                select(Run)
                .where(Run.run_id == run_id)
                .options(selectinload(Run.attempts), selectinload(Run.events))
            )
            if run is None:
                return None
            attempt_ids = [attempt.attempt_id for attempt in run.attempts]
            if attempt_ids:
                run.case_results = list(
                    session.scalars(
                        select(CaseResult).where(CaseResult.attempt_id.in_(attempt_ids))
                    )
                )
                run.metrics = list(
                    session.scalars(select(Metric).where(Metric.attempt_id.in_(attempt_ids)))
                )
                run.gates = list(
                    session.scalars(
                        select(GateEvaluation).where(GateEvaluation.attempt_id.in_(attempt_ids))
                    )
                )
                run.artifacts = list(
                    session.scalars(
                        select(Artifact).where(Artifact.attempt_id.in_(attempt_ids))
                    )
                )
            else:
                run.case_results = []
                run.metrics = []
                run.gates = []
                run.artifacts = []
            session.expunge(run)
            return run


@dataclass(frozen=True)
class ApiDependencies:
    run_service: RunService
    run_reader: RunReader
    readiness_check: Callable[[], None]


def build_dependencies(project_root: Path | None = None) -> ApiDependencies:
    root = project_root or Path(__file__).resolve().parents[3]
    settings = Settings.from_environment(root)
    registry = SuiteRegistry.from_yaml(settings.suites_config_path, settings.project_root)
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://quality_flow@localhost:5432/quality_flow",
    )
    engine = make_engine(database_url)
    session_factory = make_session_factory(engine)
    redis_client = Redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )

    def check_readiness() -> None:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
        if not redis_client.ping():
            raise RuntimeError("Redis is unavailable")
        if not registry:
            raise RuntimeError("suite registry is unavailable")

    return ApiDependencies(
        run_service=RunService(SqlAlchemyUnitOfWork(session_factory), registry),
        run_reader=SqlAlchemyRunReader(session_factory),
        readiness_check=check_readiness,
    )
