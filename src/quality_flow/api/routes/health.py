"""Liveness and dependency-aware readiness endpoints."""

from fastapi import APIRouter, HTTPException, Request

from quality_flow.api.dependencies import ApiDependencies
from quality_flow.api.schemas import HealthResponse


router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthResponse)
def live() -> HealthResponse:
    return HealthResponse(status="live")


@router.get("/health/ready", response_model=HealthResponse)
def ready(request: Request) -> HealthResponse:
    dependencies: ApiDependencies = request.app.state.dependencies
    try:
        dependencies.readiness_check()
    except Exception as error:
        raise HTTPException(status_code=503, detail="dependencies are unavailable") from error
    return HealthResponse(status="ready")
