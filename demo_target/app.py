"""A deterministic, dependency-free target for functional and load scenarios."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from threading import Lock
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


Sleep = Callable[[float], Awaitable[None]]
WorkMode = Literal["ok", "error", "slow", "baseline", "degraded"]


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


def create_app(*, sleep: Sleep = asyncio.sleep) -> FastAPI:
    target = FastAPI(title="QualityFlow Demo Target")
    resources: dict[str, dict[str, str]] = {}
    resources_lock = Lock()
    resource_counter = 0

    @target.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @target.get("/work")
    async def work(mode: WorkMode):
        if mode == "error":
            return JSONResponse(
                status_code=500, content={"mode": mode, "status": "error"}
            )
        if mode == "slow":
            await sleep(5.0)
        elif mode == "degraded":
            await sleep(0.35)
        return {"mode": mode, "status": "ok"}

    @target.post(
        "/workflow/resources", status_code=status.HTTP_201_CREATED
    )
    def create_resource(body: ResourceInput) -> dict[str, str]:
        nonlocal resource_counter
        with resources_lock:
            resource_counter += 1
            resource = {
                "id": f"resource-{resource_counter}",
                "name": body.name,
            }
            resources[resource["id"]] = resource
            return dict(resource)

    @target.get("/workflow/resources/{resource_id}")
    def get_resource(resource_id: str) -> dict[str, str]:
        with resources_lock:
            resource = resources.get(resource_id)
            if resource is None:
                raise HTTPException(status_code=404, detail="resource not found")
            return dict(resource)

    @target.patch("/workflow/resources/{resource_id}")
    def update_resource(
        resource_id: str, body: ResourceInput
    ) -> dict[str, str]:
        with resources_lock:
            resource = resources.get(resource_id)
            if resource is None:
                raise HTTPException(status_code=404, detail="resource not found")
            resource["name"] = body.name
            return dict(resource)

    @target.delete(
        "/workflow/resources/{resource_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_resource(resource_id: str) -> Response:
        with resources_lock:
            if resources.pop(resource_id, None) is None:
                raise HTTPException(status_code=404, detail="resource not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return target


app = create_app()
