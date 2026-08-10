"""A deterministic, dependency-free target for functional and load scenarios."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse


Sleep = Callable[[float], Awaitable[None]]
WorkMode = Literal["ok", "error", "slow", "baseline", "degraded"]


def create_app(*, sleep: Sleep = asyncio.sleep) -> FastAPI:
    target = FastAPI(title="QualityFlow Demo Target")

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

    return target


app = create_app()
