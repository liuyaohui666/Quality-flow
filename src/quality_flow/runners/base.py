"""Pure runner result data used by parsers and quality-gate evaluation."""

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
class CaseResultData:
    """A persisted-case compatible representation produced by a runner parser."""

    node_id: str
    status: str
    duration_ms: float
    message: str | None = None


@dataclass(frozen=True)
class PerformanceSummary:
    request_count: int
    p95_ms: float
    failure_ratio: float = 0.0
    requests_per_second: float = 0.0
    average_response_time_ms: float = 0.0
    failure_count: int = 0

    @property
    def rps(self) -> float:
        """Short alias used by performance-result consumers."""
        return self.requests_per_second

    @property
    def average_ms(self) -> float:
        """Short alias used by performance-result consumers."""
        return self.average_response_time_ms


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason_codes: tuple[str, ...]
    details: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
