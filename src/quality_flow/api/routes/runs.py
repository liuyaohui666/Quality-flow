"""Run submission and read-only lifecycle endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status

from quality_flow.api.dependencies import ApiDependencies
from quality_flow.api.schemas import (
    ArtifactsResponse,
    EventsResponse,
    RunCreateRequest,
    RunResponse,
    artifact_responses,
    event_responses,
    run_response,
)
from quality_flow.suites.registry import InvalidSuiteParameter, UnknownSuiteError


router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


def _dependencies(request: Request) -> ApiDependencies:
    return request.app.state.dependencies


def _read_run(run_id: UUID, dependencies: ApiDependencies):
    run = dependencies.run_reader.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
def create_run(
    body: RunCreateRequest,
    request: Request,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=255)
    ] = None,
) -> RunResponse:
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must be non-empty")
    dependencies = _dependencies(request)
    try:
        created = dependencies.run_service.create_run(
            body.suite_id, idempotency_key, body.parameters
        )
    except UnknownSuiteError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (InvalidSuiteParameter, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    run = dependencies.run_reader.get_run(created.run_id)
    if run is None:
        raise HTTPException(status_code=503, detail="persisted run is unavailable")
    return run_response(run)


@router.get("/{run_id}", response_model=RunResponse)
def get_run(run_id: UUID, request: Request) -> RunResponse:
    return run_response(_read_run(run_id, _dependencies(request)))


@router.get("/{run_id}/events", response_model=EventsResponse)
def get_events(run_id: UUID, request: Request) -> EventsResponse:
    return EventsResponse(events=event_responses(_read_run(run_id, _dependencies(request))))


@router.get("/{run_id}/artifacts", response_model=ArtifactsResponse)
def get_artifacts(run_id: UUID, request: Request) -> ArtifactsResponse:
    return ArtifactsResponse(
        artifacts=artifact_responses(_read_run(run_id, _dependencies(request)))
    )
