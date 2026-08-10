"""Database-free quality-gate calculations for runner summaries."""

from quality_flow.runners.base import CaseSummary, GateResult, PerformanceSummary
from quality_flow.suites.registry import GatePolicy


def evaluate_functional_gate(summary: CaseSummary, policy: GatePolicy) -> GateResult:
    """Evaluate case pass, failure, and error thresholds for one run."""
    if summary.total == 0:
        return GateResult(passed=False, reason_codes=("no_cases",), details={})

    pass_rate = summary.passed / summary.total
    error_rate = summary.errors / summary.total
    reason_codes: list[str] = []
    if pass_rate < policy.min_pass_rate:
        reason_codes.append("pass_rate")
    if summary.failed > policy.max_failures:
        reason_codes.append("failures")
    if policy.max_error_rate is not None and error_rate > policy.max_error_rate:
        reason_codes.append("error_rate")

    return GateResult(
        passed=not reason_codes,
        reason_codes=tuple(reason_codes),
        details={
            "total": float(summary.total),
            "pass_rate": pass_rate,
            "failures": float(summary.failed),
            "error_rate": error_rate,
        },
    )


def evaluate_performance_gate(
    summary: PerformanceSummary, policy: GatePolicy
) -> GateResult:
    """Evaluate configured request-volume and p95 latency thresholds."""
    reason_codes: list[str] = []
    if policy.min_requests is not None and summary.request_count < policy.min_requests:
        reason_codes.append("min_requests")
    if policy.max_p95_ms is not None and summary.p95_ms > policy.max_p95_ms:
        reason_codes.append("p95_ms")
    if (
        policy.max_error_rate is not None
        and summary.failure_ratio > policy.max_error_rate
    ):
        reason_codes.append("error_rate")

    details = {
        "request_count": float(summary.request_count),
        "p95_ms": summary.p95_ms,
    }
    if policy.max_error_rate is not None:
        details["error_rate"] = summary.failure_ratio

    return GateResult(
        passed=not reason_codes,
        reason_codes=tuple(reason_codes),
        details=details,
    )
