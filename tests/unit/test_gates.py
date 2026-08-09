from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from quality_flow.runners.base import CaseSummary, GateResult, PerformanceSummary
from quality_flow.runners.gates import (
    evaluate_functional_gate,
    evaluate_performance_gate,
)
from quality_flow.suites.registry import GatePolicy


def test_functional_gate_fails_below_required_pass_rate() -> None:
    result = evaluate_functional_gate(
        CaseSummary(total=10, passed=9, failed=1, errors=0, skipped=0),
        GatePolicy(min_pass_rate=1.0, max_failures=0),
    )

    assert result.passed is False
    assert "pass_rate" in result.reason_codes
    assert result.details["pass_rate"] == 0.9


def test_functional_gate_reports_failures_and_error_rate_independently() -> None:
    result = evaluate_functional_gate(
        CaseSummary(total=10, passed=8, failed=1, errors=1, skipped=0),
        GatePolicy(min_pass_rate=0.0, max_failures=0, max_error_rate=0.05),
    )

    assert result.passed is False
    assert result.reason_codes == ("failures", "error_rate")
    assert result.details == {
        "total": 10.0,
        "pass_rate": 0.8,
        "failures": 1.0,
        "error_rate": 0.1,
    }


def test_functional_gate_rejects_empty_summaries() -> None:
    result = evaluate_functional_gate(
        CaseSummary(total=0, passed=0, failed=0, errors=0, skipped=0), GatePolicy()
    )

    assert result.passed is False
    assert result.reason_codes == ("no_cases",)


def test_performance_gate_checks_minimum_requests_and_p95() -> None:
    result = evaluate_performance_gate(
        PerformanceSummary(request_count=99, p95_ms=250.0),
        GatePolicy(max_p95_ms=200.0, min_requests=100),
    )

    assert result.passed is False
    assert result.reason_codes == ("min_requests", "p95_ms")
    assert result.details == {"request_count": 99.0, "p95_ms": 250.0}


def test_gate_result_is_immutable_including_details() -> None:
    result = GateResult(passed=True, reason_codes=(), details={"pass_rate": 1.0})

    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]
    assert isinstance(result.details, MappingProxyType)
    with pytest.raises(TypeError):
        result.details["pass_rate"] = 0.5  # type: ignore[index]
