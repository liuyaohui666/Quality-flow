from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import time

import pytest

from quality_flow.application.worker import _PostRunLeaseKeeper
from quality_flow.runners.agent_eval_runner import AgentEvalRunner
from quality_flow.runners.workflow_runner import WorkflowRunner
from quality_flow.worker import tasks as worker_tasks


def test_lease_keeper_stop_detaches_from_one_blocked_heartbeat() -> None:
    callback_started = Event()
    release_callback = Event()
    stop_returned = Event()
    count_lock = Lock()
    call_count = 0
    active_callbacks = 0
    max_active_callbacks = 0

    def heartbeat() -> None:
        nonlocal call_count, active_callbacks, max_active_callbacks
        with count_lock:
            call_count += 1
            active_callbacks += 1
            max_active_callbacks = max(max_active_callbacks, active_callbacks)
            current_call = call_count
        try:
            if current_call > 1:
                callback_started.set()
                release_callback.wait()
        finally:
            with count_lock:
                active_callbacks -= 1

    keeper = _PostRunLeaseKeeper(
        heartbeat,
        interval_seconds=0.01,
        thread_name="test-blocked-lease-keeper",
    )
    keeper.__enter__()
    assert callback_started.wait(timeout=1)

    def stop_keeper() -> None:
        keeper.stop()
        stop_returned.set()

    stopped_before_release = False
    with ThreadPoolExecutor(max_workers=1) as executor:
        stopped = executor.submit(stop_keeper)
        try:
            stopped_before_release = stop_returned.wait(timeout=0.5)
        finally:
            release_callback.set()
            stopped.result(timeout=1)

    assert stopped_before_release is True
    assert call_count == 2
    assert max_active_callbacks == 1


def test_lease_keeper_preserves_context_error_over_heartbeat_error() -> None:
    heartbeat_failure = RuntimeError("late heartbeat failure")
    context_failure = ValueError("runner failure")
    count_lock = Lock()
    call_count = 0

    def heartbeat() -> None:
        nonlocal call_count
        with count_lock:
            call_count += 1
            current_call = call_count
        if current_call > 1:
            raise heartbeat_failure

    keeper = _PostRunLeaseKeeper(
        heartbeat,
        interval_seconds=0.01,
        thread_name="test-failing-lease-keeper",
    )

    with pytest.raises(ValueError) as raised:
        with keeper:
            deadline = time.monotonic() + 1
            while True:
                try:
                    keeper.raise_if_failed()
                except RuntimeError as error:
                    assert error is heartbeat_failure
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("heartbeat failure did not complete")
                time.sleep(0.01)
            raise context_failure

    assert raised.value is context_failure


def test_lease_keeper_propagates_completed_heartbeat_error_by_identity() -> None:
    failure = RuntimeError("completed heartbeat failure")
    count_lock = Lock()
    call_count = 0

    def heartbeat() -> None:
        nonlocal call_count
        with count_lock:
            call_count += 1
            current_call = call_count
        if current_call > 1:
            raise failure

    keeper = _PostRunLeaseKeeper(
        heartbeat,
        interval_seconds=0.01,
        thread_name="test-identity-lease-keeper",
    )
    keeper.__enter__()

    observed = None
    deadline = time.monotonic() + 1
    while observed is None and time.monotonic() < deadline:
        try:
            keeper.raise_if_failed()
        except RuntimeError as error:
            observed = error
        else:
            time.sleep(0.01)

    with pytest.raises(RuntimeError) as raised:
        keeper.stop()

    assert observed is failure
    assert raised.value is failure


def test_default_worker_registers_workflow_with_only_the_target_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUALITY_FLOW_TARGET_URL", "http://demo-target:8001")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-exposed")
    monkeypatch.setattr(worker_tasks, "make_engine", lambda _url: object())
    monkeypatch.setattr(
        worker_tasks, "make_session_factory", lambda _engine: object()
    )
    worker_tasks.build_default_worker.cache_clear()


def test_default_worker_registers_agent_eval_with_only_target_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QUALITY_FLOW_TARGET_URL", "http://demo-target:8001")
    monkeypatch.setenv("SECRET_TOKEN", "must-not-be-forwarded")
    worker_tasks.build_default_worker.cache_clear()

    worker = worker_tasks.build_default_worker()
    runner = worker._runners["agent_eval"]

    assert isinstance(runner, AgentEvalRunner)
    assert runner._environment == {
        "QUALITY_FLOW_TARGET_URL": "http://demo-target:8001"
    }
    worker_tasks.build_default_worker.cache_clear()

    worker = worker_tasks.build_default_worker()
    workflow = worker._runners["workflow"]

    assert isinstance(workflow, WorkflowRunner)
    assert workflow._environment == {
        "QUALITY_FLOW_TARGET_URL": "http://demo-target:8001"
    }
    worker_tasks.build_default_worker.cache_clear()
