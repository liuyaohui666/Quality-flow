"""At-least-once delivery of PostgreSQL outbox messages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class OutboxMessage:
    event_id: UUID
    run_id: UUID
    publish_attempts: int
    published_at: datetime | None


class OutboxStore(Protocol):
    def pending(self, limit: int) -> tuple[OutboxMessage, ...]: ...

    def record_publish_attempt(self, event_id: UUID) -> bool: ...

    def mark_published(self, event_id: UUID, published_at: datetime) -> bool: ...


Publisher = Callable[..., None]


class OutboxDispatcher:
    def __init__(
        self,
        store: OutboxStore,
        publisher: Publisher,
        *,
        batch_size: int = 100,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._batch_size = batch_size
        self._clock = clock or (lambda: datetime.now(UTC))

    def dispatch_once(self) -> int:
        published = 0
        for message in self._store.pending(self._batch_size):
            if not self._store.record_publish_attempt(message.event_id):
                continue
            try:
                self._publisher(event_id=message.event_id, run_id=message.run_id)
            except Exception:
                continue
            if self._store.mark_published(message.event_id, self._clock()):
                published += 1
        return published
