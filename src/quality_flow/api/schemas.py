"""Public, path-free control-plane request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: str = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict)


class TimestampsResponse(BaseModel):
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AttemptResponse(BaseModel):
    attempt_id: UUID
    attempt_no: int
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None


class CaseSummaryResponse(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0


class MetricResponse(BaseModel):
    name: str
    value: float
    unit: str | None


class GateResponse(BaseModel):
    gate_type: str
    passed: bool
    reason_codes: list[str]


class ArtifactResponse(BaseModel):
    artifact_id: UUID
    attempt_id: UUID
    artifact_type: str
    checksum: str | None
    size_bytes: int | None
    mime_type: str | None
    created_at: datetime


class RunResponse(BaseModel):
    run_id: UUID
    suite_id: str
    status: str
    outcome: str
    timestamps: TimestampsResponse
    attempts: list[AttemptResponse]
    case_summary: CaseSummaryResponse
    metrics: list[MetricResponse]
    gates: list[GateResponse]
    artifacts: list[ArtifactResponse]


class RunEventResponse(BaseModel):
    event_id: UUID
    event_type: str
    status: str | None = None
    outcome: str | None = None
    created_at: datetime


class EventsResponse(BaseModel):
    events: list[RunEventResponse]


class ArtifactsResponse(BaseModel):
    artifacts: list[ArtifactResponse]


class HealthResponse(BaseModel):
    status: str


def run_response(run: Any) -> RunResponse:
    cases = list(getattr(run, "case_results", []))
    summary = CaseSummaryResponse(
        total=len(cases),
        passed=sum(case.status == "passed" for case in cases),
        failed=sum(case.status == "failed" for case in cases),
        errors=sum(case.status == "error" for case in cases),
        skipped=sum(case.status == "skipped" for case in cases),
    )
    return RunResponse(
        run_id=run.run_id,
        suite_id=run.suite_id,
        status=str(run.status),
        outcome=str(run.outcome),
        timestamps=TimestampsResponse(
            created_at=run.created_at,
            updated_at=run.updated_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        ),
        attempts=[
            AttemptResponse(
                attempt_id=attempt.attempt_id,
                attempt_no=attempt.attempt_no,
                status=str(attempt.status),
                created_at=attempt.created_at,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                exit_code=attempt.exit_code,
            )
            for attempt in getattr(run, "attempts", [])
        ],
        case_summary=summary,
        metrics=[
            MetricResponse(name=metric.metric_name, value=metric.metric_value, unit=metric.unit)
            for metric in getattr(run, "metrics", [])
        ],
        gates=[
            GateResponse(
                gate_type=gate.gate_type,
                passed=gate.passed,
                reason_codes=list(gate.reason_codes),
            )
            for gate in getattr(run, "gates", [])
        ],
        artifacts=artifact_responses(run),
    )


def artifact_responses(run: Any) -> list[ArtifactResponse]:
    """Deliberately omit artifact URIs and arbitrary metadata from the API."""
    responses: list[ArtifactResponse] = []
    for artifact in getattr(run, "artifacts", []):
        metadata = artifact.artifact_metadata
        responses.append(
            ArtifactResponse(
                artifact_id=artifact.artifact_id,
                attempt_id=artifact.attempt_id,
                artifact_type=artifact.artifact_type,
                checksum=artifact.checksum,
                size_bytes=metadata.get("size_bytes"),
                mime_type=metadata.get("mime_type"),
                created_at=artifact.created_at,
            )
        )
    return responses


def event_responses(run: Any) -> list[RunEventResponse]:
    """Expose only known lifecycle attributes, not arbitrary event payloads."""
    return [
        RunEventResponse(
            event_id=event.event_id,
            event_type=event.event_type,
            status=event.payload.get("status"),
            outcome=event.payload.get("outcome"),
            created_at=event.created_at,
        )
        for event in sorted(getattr(run, "events", []), key=lambda item: item.created_at)
    ]
