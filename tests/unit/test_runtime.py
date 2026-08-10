from __future__ import annotations

from threading import Event

from quality_flow import runtime


def test_poll_loop_runs_actions_until_stop_is_requested() -> None:
    stop = Event()
    calls: list[str] = []

    def action() -> int:
        calls.append("poll")
        if len(calls) == 2:
            stop.set()
        return 0

    runtime.poll_until_stopped(action, stop=stop, interval_seconds=0.01)

    assert calls == ["poll", "poll"]


def test_poll_loop_logs_failure_and_keeps_authoritative_polling_state(caplog) -> None:
    stop = Event()
    attempts = 0

    def action() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient transport failure")
        stop.set()
        return 1

    runtime.poll_until_stopped(action, stop=stop, interval_seconds=0.01)

    assert attempts == 2
    assert "poll failed" in caplog.text


def test_main_dispatches_only_known_long_running_roles(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runtime, "run_dispatcher", lambda: calls.append("dispatcher"))
    monkeypatch.setattr(runtime, "run_reconciler", lambda: calls.append("reconciler"))

    assert runtime.main(["dispatcher"]) == 0
    assert runtime.main(["reconciler"]) == 0
    assert calls == ["dispatcher", "reconciler"]
