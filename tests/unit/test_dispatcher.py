from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from quality_flow.application.dispatcher import OutboxDispatcher, OutboxMessage
from quality_flow.infrastructure.celery_app import (
    EXECUTE_RUN_TASK,
    CeleryRunPublisher,
    create_celery_app,
)
from quality_flow.worker.tasks import create_worker_celery_app


class MemoryOutboxStore:
    def __init__(self, message: OutboxMessage) -> None:
        self.message = message

    def pending(self, limit: int) -> tuple[OutboxMessage, ...]:
        if self.message.published_at is not None:
            return ()
        return (self.message,)

    def record_publish_attempt(self, event_id):
        if self.message.event_id != event_id or self.message.published_at is not None:
            return False
        self.message = replace(
            self.message, publish_attempts=self.message.publish_attempts + 1
        )
        return True

    def mark_published(self, event_id, published_at):
        if self.message.event_id != event_id or self.message.published_at is not None:
            return False
        self.message = replace(self.message, published_at=published_at)
        return True


def test_publish_failure_stays_pending_and_increments_attempts() -> None:
    message = OutboxMessage(
        event_id=uuid4(),
        run_id=uuid4(),
        publish_attempts=0,
        published_at=None,
    )
    store = MemoryOutboxStore(message)

    def fail_publish(*, event_id, run_id) -> None:
        raise ConnectionError("broker unavailable")

    dispatcher = OutboxDispatcher(
        store,
        fail_publish,
        clock=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )

    assert dispatcher.dispatch_once() == 0
    assert store.message.published_at is None
    assert store.message.publish_attempts == 1


def test_outbox_is_still_pending_while_publisher_is_called() -> None:
    message = OutboxMessage(uuid4(), uuid4(), 0, None)
    store = MemoryOutboxStore(message)
    observed_published_at = []

    def observe_publish(*, event_id, run_id) -> None:
        observed_published_at.append(store.message.published_at)

    dispatcher = OutboxDispatcher(
        store,
        observe_publish,
        clock=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )

    assert dispatcher.dispatch_once() == 1
    assert observed_published_at == [None]
    assert store.message.published_at == datetime(2026, 8, 10, tzinfo=UTC)


def test_unknown_publish_outcome_reuses_stable_event_and_run_ids() -> None:
    message = OutboxMessage(uuid4(), uuid4(), 0, None)
    store = MemoryOutboxStore(message)
    calls = []

    def publish_then_lose_ack(*, event_id, run_id) -> None:
        calls.append((event_id, run_id))
        if len(calls) == 1:
            raise TimeoutError("publish acknowledgement unknown")

    dispatcher = OutboxDispatcher(
        store,
        publish_then_lose_ack,
        clock=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )

    assert dispatcher.dispatch_once() == 0
    assert dispatcher.dispatch_once() == 1
    assert calls == [(message.event_id, message.run_id)] * 2
    assert store.message.publish_attempts == 2


def test_broker_success_followed_by_mark_commit_crash_republishes_same_ids() -> None:
    message = OutboxMessage(uuid4(), uuid4(), 0, None)

    class CommitCrashStore(MemoryOutboxStore):
        def __init__(self, initial):
            super().__init__(initial)
            self.fail_mark = True

        def mark_published(self, event_id, published_at):
            if self.fail_mark:
                self.fail_mark = False
                raise RuntimeError("database commit outcome unknown")
            return super().mark_published(event_id, published_at)

    store = CommitCrashStore(message)
    calls = []

    def publish(*, event_id, run_id) -> None:
        calls.append((event_id, run_id))

    dispatcher = OutboxDispatcher(store, publish)

    with pytest.raises(RuntimeError, match="commit outcome unknown"):
        dispatcher.dispatch_once()
    assert store.message.published_at is None
    assert dispatcher.dispatch_once() == 1
    assert calls == [(message.event_id, message.run_id)] * 2


def test_celery_uses_json_no_results_low_prefetch_and_late_ack() -> None:
    app = create_celery_app("redis://127.0.0.1:6379/14", queue_name="qf-test")

    assert app.conf.task_serializer == "json"
    assert app.conf.accept_content == ["json"]
    assert app.conf.result_backend is None
    assert app.conf.task_ignore_result is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.task_default_queue == "qf-test"
    assert app.conf.task_default_exchange == "qf-test"
    assert app.conf.task_default_routing_key == "qf-test"


class CapturingCeleryApp:
    def __init__(self) -> None:
        self.calls = []

    def send_task(self, name, **options):
        self.calls.append((name, options))


def test_celery_message_contains_only_identifiers_and_stable_message_id() -> None:
    app = CapturingCeleryApp()
    event_id = uuid4()
    run_id = uuid4()

    CeleryRunPublisher(app, queue_name="qf-isolated").publish(
        event_id=event_id, run_id=run_id
    )

    assert app.calls == [
        (
            EXECUTE_RUN_TASK,
            {
                "kwargs": {"event_id": str(event_id), "run_id": str(run_id)},
                "task_id": str(event_id),
                "queue": "qf-isolated",
            },
        )
    ]


class CapturingWorker:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, run_id, *, worker_id=None):
        self.calls.append((run_id, worker_id))
        return True


def test_registered_celery_task_accepts_only_event_and_run_identifiers() -> None:
    worker = CapturingWorker()
    app = create_worker_celery_app(
        "memory://", queue_name="qf-task-contract", worker_factory=lambda: worker
    )
    event_id = uuid4()
    run_id = uuid4()

    task = app.tasks[EXECUTE_RUN_TASK]
    assert task.run(event_id=str(event_id), run_id=str(run_id)) is True

    assert worker.calls == [(run_id, None)]
