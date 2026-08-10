"""Celery task registration and injectable worker construction."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from functools import lru_cache
import os
from pathlib import Path
import tempfile
from uuid import UUID

from celery import Celery

from quality_flow.application.worker import RunWorker
from quality_flow.infrastructure.artifacts import FileArtifactStore
from quality_flow.infrastructure.celery_app import EXECUTE_RUN_TASK, create_celery_app
from quality_flow.infrastructure.database import make_engine, make_session_factory
from quality_flow.runners.locust_runner import LocustRunner
from quality_flow.runners.pytest_runner import PytestRunner


WorkerFactory = Callable[[], RunWorker]


def create_worker_celery_app(
    broker_url: str,
    *,
    queue_name: str = "quality-flow",
    worker_factory: WorkerFactory | None = None,
) -> Celery:
    app = create_celery_app(broker_url, queue_name=queue_name)
    provide_worker = worker_factory or build_default_worker

    @app.task(
        name=EXECUTE_RUN_TASK,
        bind=True,
        ignore_result=True,
        acks_late=True,
        shared=False,
    )
    def execute_run(self, *, event_id: str, run_id: str) -> bool:
        UUID(event_id)
        parsed_run_id = UUID(run_id)
        worker_id = getattr(self.request, "hostname", None)
        return provide_worker().execute(parsed_run_id, worker_id=worker_id)

    return app


@lru_cache(maxsize=1)
def build_default_worker() -> RunWorker:
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://quality_flow@localhost:5432/quality_flow",
    )
    service_root = Path(
        os.environ.get(
            "QUALITY_FLOW_RUNTIME_ROOT",
            str(Path(tempfile.gettempdir()) / "quality-flow"),
        )
    )
    workspace_root = Path(
        os.environ.get(
            "QUALITY_FLOW_WORKSPACE_ROOT", str(service_root / "workspaces")
        )
    )
    staging_root = Path(
        os.environ.get("QUALITY_FLOW_STAGING_ROOT", str(service_root / "staging"))
    )
    artifact_root = Path(
        os.environ.get("QUALITY_FLOW_ARTIFACT_ROOT", str(service_root / "artifacts"))
    )
    lease_seconds = int(os.environ.get("QUALITY_FLOW_LEASE_SECONDS", "30"))
    session_factory = make_session_factory(make_engine(database_url))
    return RunWorker(
        session_factory,
        runners={
            "pytest": PytestRunner(staging_root=staging_root),
            "locust": LocustRunner(staging_root=staging_root),
        },
        artifact_store=FileArtifactStore(artifact_root),
        workspace_root=workspace_root,
        staging_root=staging_root,
        lease_duration=timedelta(seconds=lease_seconds),
    )


celery_app = create_worker_celery_app(
    os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    queue_name=os.environ.get("QUALITY_FLOW_QUEUE", "quality-flow"),
)
