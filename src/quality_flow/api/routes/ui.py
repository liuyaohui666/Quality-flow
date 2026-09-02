"""Browser entry points for the lightweight QualityFlow console."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse


STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"
router = APIRouter(include_in_schema=False)


@router.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/", status_code=307)


@router.get("/ui/")
def console() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html", media_type="text/html")
