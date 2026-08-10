"""Short PostgreSQL transactions used by the outbox dispatcher."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update

from quality_flow.application.dispatcher import OutboxMessage
from quality_flow.infrastructure.database import SessionFactory
from quality_flow.infrastructure.models import OutboxEvent


class SqlAlchemyOutboxStore:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def pending(self, limit: int) -> tuple[OutboxMessage, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.created_at, OutboxEvent.outbox_event_id)
                .limit(limit)
            ).all()
            return tuple(
                OutboxMessage(
                    event_id=row.outbox_event_id,
                    run_id=row.aggregate_id,
                    publish_attempts=row.publish_attempts,
                    published_at=row.published_at,
                )
                for row in rows
            )

    def record_publish_attempt(self, event_id: UUID) -> bool:
        with self._session_factory.begin() as session:
            result = session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.outbox_event_id == event_id,
                    OutboxEvent.published_at.is_(None),
                )
                .values(publish_attempts=OutboxEvent.publish_attempts + 1)
            )
            return result.rowcount == 1

    def mark_published(self, event_id: UUID, published_at: datetime) -> bool:
        with self._session_factory.begin() as session:
            result = session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.outbox_event_id == event_id,
                    OutboxEvent.published_at.is_(None),
                )
                .values(published_at=published_at)
            )
            return result.rowcount == 1
