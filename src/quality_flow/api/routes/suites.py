"""Safe catalog of pre-registered test suites."""

from fastapi import APIRouter, Request

from quality_flow.api.dependencies import ApiDependencies
from quality_flow.api.schemas import SuiteResponse, SuitesResponse


router = APIRouter(prefix="/api/v1/suites", tags=["suites"])


@router.get("", response_model=SuitesResponse)
def get_suites(request: Request) -> SuitesResponse:
    dependencies: ApiDependencies = request.app.state.dependencies
    return SuitesResponse(
        suites=[
            SuiteResponse(
                suite_id=suite.suite_id,
                runner_type=suite.runner_type,
                allowed_parameters={
                    name: list(values)
                    for name, values in suite.allowed_parameters.items()
                },
            )
            for suite in dependencies.suite_definitions
        ]
    )
