"""Internal bounded callback dispatch shared by execution supervisors."""

from __future__ import annotations

from collections.abc import Callable
import queue
import threading


class BoundedCallbackCall:
    """One cancellable callback invocation owned by the bounded dispatcher."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.error: BaseException | None = None
        self._started = False
        self._lock = threading.Lock()

    def run(self) -> None:
        with self._lock:
            if self.cancelled.is_set():
                self.done.set()
                return
            self._started = True
        error: BaseException | None = None
        try:
            self.callback()
        except BaseException as callback_error:
            error = callback_error
        finally:
            with self._lock:
                self.error = error
                self.done.set()

    def cancel(self) -> BaseException | None:
        """Cancel a queued call and return an already-completed error."""
        with self._lock:
            self.cancelled.set()
            if not self._started:
                self.done.set()
            return self.error


class BoundedCallbackDispatcher:
    """Process-wide bounded daemon pool for callbacks that may block forever."""

    def __init__(self, *, worker_count: int = 4, queue_capacity: int = 32) -> None:
        self._worker_count = worker_count
        self._queue: queue.Queue[BoundedCallbackCall] = queue.Queue(
            maxsize=queue_capacity
        )
        self._start_lock = threading.Lock()
        self._started = False

    def submit(self, callback: Callable[[], None]) -> BoundedCallbackCall:
        self._ensure_started()
        call = BoundedCallbackCall(callback)
        try:
            self._queue.put_nowait(call)
        except queue.Full:
            call.error = RuntimeError("heartbeat dispatcher capacity exhausted")
            call.done.set()
        return call

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            for index in range(self._worker_count):
                threading.Thread(
                    target=self._worker,
                    name=f"quality-flow-heartbeat-{index}",
                    daemon=True,
                ).start()
            self._started = True

    def _worker(self) -> None:
        while True:
            self._queue.get().run()


BOUNDED_CALLBACK_DISPATCHER = BoundedCallbackDispatcher()
