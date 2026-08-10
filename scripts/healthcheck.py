"""Bounded liveness checks for long-running QualityFlow roles."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import stat
import time
from typing import Any, Callable, Sequence

from redis import Redis


def heartbeat_is_recent(
    path: Path, *, max_age_seconds: float, now: float | None = None
) -> bool:
    if max_age_seconds <= 0:
        return False
    try:
        status = path.stat()
    except OSError:
        return False
    if not stat.S_ISREG(status.st_mode):
        return False
    checked_at = time.time() if now is None else now
    return 0 <= checked_at - status.st_mtime <= max_age_seconds


def worker_is_responsive(app: Any, *, hostname: str, timeout: float) -> bool:
    if timeout <= 0:
        return False
    worker_name = f"worker@{hostname}"
    replies = app.control.ping(destination=[worker_name], timeout=timeout)
    return any(worker_name in reply for reply in replies or [])


def dispatcher_is_responsive(
    heartbeat_path: Path,
    *,
    redis_url: str,
    max_age_seconds: float,
    timeout: float,
    now: float | None = None,
    client_factory: Callable[..., Any] = Redis.from_url,
) -> bool:
    if not heartbeat_is_recent(
        heartbeat_path, max_age_seconds=max_age_seconds, now=now
    ):
        return False
    client = client_factory(
        redis_url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    try:
        return bool(client.ping())
    finally:
        client.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="role", required=True)
    heartbeat = subparsers.add_parser("heartbeat")
    heartbeat.add_argument("path", type=Path)
    heartbeat.add_argument("--max-age", type=float, default=10.0)
    dispatcher = subparsers.add_parser("dispatcher")
    dispatcher.add_argument("path", type=Path)
    dispatcher.add_argument("--max-age", type=float, default=10.0)
    dispatcher.add_argument("--timeout", type=float, default=2.0)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args(argv)

    try:
        if args.role == "heartbeat":
            healthy = heartbeat_is_recent(
                args.path, max_age_seconds=args.max_age
            )
        elif args.role == "dispatcher":
            healthy = dispatcher_is_responsive(
                args.path,
                redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                max_age_seconds=args.max_age,
                timeout=args.timeout,
            )
        else:
            from quality_flow.worker.tasks import celery_app

            try:
                healthy = worker_is_responsive(
                    celery_app,
                    hostname=os.environ.get("HOSTNAME", socket.gethostname()),
                    timeout=args.timeout,
                )
            finally:
                celery_app.close()
    except Exception:
        return 1
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
