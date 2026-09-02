"""FastAPI application factory for the QualityFlow control plane."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from quality_flow.api.dependencies import ApiDependencies, build_dependencies
from quality_flow.api.routes.health import router as health_router
from quality_flow.api.routes.runs import router as runs_router
from quality_flow.api.routes.suites import router as suites_router
from quality_flow.api.routes.ui import STATIC_ROOT, router as ui_router


def create_app(dependencies: ApiDependencies | None = None) -> FastAPI:
    app = FastAPI(title="QualityFlow", version="0.1.0")
    app.state.dependencies = dependencies or build_dependencies()
    app.include_router(health_router)
    app.include_router(runs_router)
    app.include_router(suites_router)
    app.include_router(ui_router)
    app.mount("/ui/assets", StaticFiles(directory=STATIC_ROOT), name="ui-assets")
    return app


app = create_app()
