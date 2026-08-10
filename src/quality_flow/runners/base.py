"""Pure runner result data used by parsers and quality-gate evaluation."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import math
from pathlib import Path
from types import MappingProxyType

from quality_flow.domain.enums import AttemptStatus
from quality_flow.suites.registry import GatePolicy


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


@dataclass(frozen=True)
class ExecutionSpec:
    """Immutable subset of a suite snapshot needed by a runner adapter."""

    argv: tuple[str, ...]
    timeout_seconds: float
    allowed_workspace_root: Path
    parameters: Mapping[str, str] = field(default_factory=dict)
    gate_policy: GatePolicy = field(default_factory=GatePolicy)

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        if not self.argv or not all(
            isinstance(argument, str) and argument for argument in self.argv
        ):
            raise ValueError("argv must contain non-empty strings")
        if not all(
            isinstance(name, str)
            and isinstance(value, str)
            for name, value in self.parameters.items()
        ):
            raise ValueError("parameters must map strings to strings")
        allowed_workspace_root = Path(self.allowed_workspace_root)
        if any(part == ".." for part in allowed_workspace_root.parts):
            raise ValueError("allowed_workspace_root must not contain parent traversal")
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(
            self,
            "allowed_workspace_root",
            allowed_workspace_root,
        )
        object.__setattr__(
            self, "parameters", MappingProxyType(dict(self.parameters))
        )


@dataclass(frozen=True)
class RunnerArtifact:
    """A workspace file that the worker may register with ArtifactStore."""

    artifact_type: str
    source_path: Path
    source_root: Path
    mime_type: str

    def __post_init__(self) -> None:
        if not self.artifact_type.strip():
            raise ValueError("artifact_type must not be blank")
        try:
            source_root = Path(self.source_root).resolve(strict=True)
            source_path = Path(self.source_path).resolve(strict=True)
            source_path.relative_to(source_root)
        except (OSError, ValueError) as error:
            raise ValueError("artifact source must be inside its source_root") from error
        if not source_root.is_dir() or not source_path.is_file():
            raise ValueError("artifact source_root and source_path must exist")
        object.__setattr__(self, "source_root", source_root)
        object.__setattr__(self, "source_path", source_path)


@dataclass(frozen=True)
class RunnerOutcome:
    """Runner-neutral terminal data consumed by the worker transaction."""

    attempt_status: AttemptStatus
    exit_code: int | None
    started_at: datetime
    finished_at: datetime
    case_results: tuple[CaseResultData, ...] = ()
    case_summary: CaseSummary | None = None
    performance_summary: PerformanceSummary | None = None
    gate_result: GateResult | None = None
    artifacts: tuple[RunnerArtifact, ...] = ()
    failure_kind: str | None = None
    failure_summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_results", tuple(self.case_results))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
