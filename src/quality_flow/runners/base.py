"""Pure runner result data used by quality-gate evaluation."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class CaseSummary:
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int


@dataclass(frozen=True)
class PerformanceSummary:
    request_count: int
    p95_ms: float
    failure_ratio: float = 0.0
    requests_per_second: float = 0.0
    average_ms: float = 0.0


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason_codes: tuple[str, ...]
    details: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
