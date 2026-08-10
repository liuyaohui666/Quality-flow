from __future__ import annotations

import os
from pathlib import Path

from scripts import healthcheck


def test_heartbeat_health_requires_a_recent_regular_file(tmp_path: Path) -> None:
    heartbeat = tmp_path / "dispatcher"
    heartbeat.write_text("", encoding="utf-8")
    os.utime(heartbeat, (90.0, 90.0))

    assert healthcheck.heartbeat_is_recent(heartbeat, max_age_seconds=15, now=100.0)
    assert not healthcheck.heartbeat_is_recent(
        heartbeat, max_age_seconds=5, now=100.0
    )
    assert not healthcheck.heartbeat_is_recent(
        tmp_path / "missing", max_age_seconds=15, now=100.0
    )


def test_worker_health_uses_bounded_ping_for_the_exact_worker_node() -> None:
    class Control:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], float]] = []

        def ping(self, *, destination, timeout):
            self.calls.append((destination, timeout))
            return [{"worker@abc123": {"ok": "pong"}}]

    class App:
        control = Control()

    app = App()

    assert healthcheck.worker_is_responsive(app, hostname="abc123", timeout=2.0)
    assert app.control.calls == [(["worker@abc123"], 2.0)]


def test_dispatcher_health_requires_recent_heartbeat_and_bounded_redis_ping(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "dispatcher"
    heartbeat.write_text("", encoding="utf-8")
    os.utime(heartbeat, (95.0, 95.0))
    captured: dict[str, object] = {}

    class Client:
        def ping(self) -> bool:
            captured["ping"] = True
            return True

        def close(self) -> None:
            captured["closed"] = True

    def client_factory(url: str, **kwargs):
        captured.update(url=url, **kwargs)
        return Client()

    assert healthcheck.dispatcher_is_responsive(
        heartbeat,
        redis_url="redis://redis:6379/0",
        max_age_seconds=10,
        timeout=2.0,
        now=100.0,
        client_factory=client_factory,
    )
    assert captured == {
        "url": "redis://redis:6379/0",
        "socket_connect_timeout": 2.0,
        "socket_timeout": 2.0,
        "ping": True,
        "closed": True,
    }
