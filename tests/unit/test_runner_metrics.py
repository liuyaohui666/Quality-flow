from datetime import UTC, datetime
import math

import pytest

from quality_flow.domain.enums import AttemptStatus
from quality_flow.infrastructure.repositories import _outcome_metrics
from quality_flow.runners.base import MetricData, RunnerOutcome


def test_runner_outcome_exposes_generic_metrics_for_persistence() -> None:
    now = datetime.now(UTC)
    outcome = RunnerOutcome(
        attempt_status=AttemptStatus.PASSED,
        exit_code=0,
        started_at=now,
        finished_at=now,
        metrics=(
            MetricData("agent_pass_rate", 0.75, "ratio"),
            MetricData("total_tokens", 42, "count"),
        ),
    )

    assert _outcome_metrics(outcome) == (
        ("agent_pass_rate", 0.75, "ratio"),
        ("total_tokens", 42.0, "count"),
    )


@pytest.mark.parametrize("name,value,unit", [("", 1, "count"), ("x", math.nan, "count"), ("x", 1, "")])
def test_metric_data_rejects_invalid_values(name: str, value: float, unit: str) -> None:
    with pytest.raises(ValueError):
        MetricData(name, value, unit)
