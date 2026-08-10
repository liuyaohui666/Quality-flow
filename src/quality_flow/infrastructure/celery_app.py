"""Celery transport configuration for identifier-only run messages."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from celery import Celery


EXECUTE_RUN_TASK = "quality_flow.execute_run"


def create_celery_app(broker_url: str, *, queue_name: str = "quality-flow") -> Celery:
    app = Celery("quality_flow", broker=broker_url)
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_backend=None,
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_default_queue=queue_name,
    )
    return app


class CelerySender(Protocol):
    def send_task(self, name: str, **options: object) -> object: ...


class CeleryRunPublisher:
    def __init__(self, app: CelerySender, *, queue_name: str = "quality-flow") -> None:
        self._app = app
        self._queue_name = queue_name

    def publish(self, *, event_id: UUID, run_id: UUID) -> None:
        self._app.send_task(
            EXECUTE_RUN_TASK,
            kwargs={"event_id": str(event_id), "run_id": str(run_id)},
            task_id=str(event_id),
            queue=self._queue_name,
        )
