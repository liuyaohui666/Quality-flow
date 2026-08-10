"""Long-running dispatcher and reconciler process entry points."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import timedelta
import logging
import os
import signal
from threading import Event

from quality_flow.application.dispatcher import OutboxDispatcher
from quality_flow.application.reconciler import LeaseReconciler
from quality_flow.infrastructure.celery_app import CeleryRunPublisher, create_celery_app
from quality_flow.infrastructure.database import make_engine, make_session_factory
from quality_flow.infrastructure.outbox import SqlAlchemyOutboxStore


LOGGER = logging.getLogger(__name__)
PollAction = Callable[[], int]


def poll_until_stopped(
    action: PollAction,
    *,
    stop: Event,
    interval_seconds: float,
) -> None:
    """Poll a short transaction boundary until SIGTERM/SIGINT requests shutdown."""
    if interval_seconds <= 0:
        raise ValueError("poll interval must be positive")
    while not stop.is_set():
        try:
            action()
        except Exception:
            LOGGER.exception("poll failed; authoritative state remains in PostgreSQL")
        stop.wait(interval_seconds)


def _install_shutdown_handlers(stop: Event) -> None:
    def request_shutdown(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)


def run_dispatcher() -> None:
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://quality_flow:quality_flow@localhost:5432/quality_flow",
    )
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    queue_name = os.environ.get("QUALITY_FLOW_QUEUE", "quality-flow")
    interval = float(os.environ.get("QUALITY_FLOW_DISPATCH_INTERVAL_SECONDS", "0.5"))
    engine = make_engine(database_url)
    session_factory = make_session_factory(engine)
    celery_app = create_celery_app(redis_url, queue_name=queue_name)
    publisher = CeleryRunPublisher(celery_app, queue_name=queue_name)
    dispatcher = OutboxDispatcher(
        SqlAlchemyOutboxStore(session_factory), publisher.publish
    )
    stop = Event()
    _install_shutdown_handlers(stop)
    try:
        poll_until_stopped(
            dispatcher.dispatch_once, stop=stop, interval_seconds=interval
        )
    finally:
        celery_app.close()
        engine.dispose()


def run_reconciler() -> None:
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://quality_flow:quality_flow@localhost:5432/quality_flow",
    )
    interval = float(os.environ.get("QUALITY_FLOW_RECONCILE_INTERVAL_SECONDS", "2"))
    lease_seconds = float(os.environ.get("QUALITY_FLOW_LEASE_SECONDS", "30"))
    engine = make_engine(database_url)
    session_factory = make_session_factory(engine)
    reconciler = LeaseReconciler(session_factory)
    stop = Event()
    _install_shutdown_handlers(stop)
    try:
        poll_until_stopped(
            reconciler.reconcile_once,
            stop=stop,
            interval_seconds=min(interval, max(0.1, lease_seconds / 2)),
        )
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("dispatcher", "reconciler"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if args.role == "dispatcher":
        run_dispatcher()
    else:
        run_reconciler()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
