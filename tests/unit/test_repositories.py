from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from quality_flow.infrastructure.repositories import RunRepository


class DriverFailure(Exception):
    sqlstate = "08006"


def test_terminal_lock_preserves_non_lock_operational_error_identity() -> None:
    failure = OperationalError("SELECT runs", {}, DriverFailure("connection lost"))

    class FailingSession:
        def execute(self, _statement):
            return None

        def scalar(self, _statement):
            raise failure

    repository = RunRepository(FailingSession())  # type: ignore[arg-type]

    with pytest.raises(OperationalError) as raised:
        repository._lock_live_attempt(
            uuid4(),
            uuid4(),
            uuid4(),
            datetime(2026, 8, 10, tzinfo=UTC),
        )

    assert raised.value is failure
