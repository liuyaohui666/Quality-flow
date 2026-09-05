"""A deterministic, dependency-free target for functional and load scenarios."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from threading import Lock
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


Sleep = Callable[[float], Awaitable[None]]
WorkMode = Literal["ok", "error", "slow", "baseline", "degraded"]


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    messages: tuple[AgentMessage, ...] | None = Field(
        default=None, min_length=1, max_length=39
    )

    @model_validator(mode="after")
    def validate_request_mode(self) -> "AgentRequest":
        if (self.prompt is None) == (self.messages is None):
            raise ValueError("exactly one of prompt or messages is required")
        if self.messages is not None:
            roles = [message.role for message in self.messages]
            if roles[0] != "user" or roles[-1] != "user":
                raise ValueError("conversation must start and end with a user message")
            if any(current == previous for previous, current in zip(roles, roles[1:])):
                raise ValueError("conversation roles must alternate")
        return self


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

    @target.post("/agent/respond")
    def agent_respond(body: AgentRequest) -> dict[str, object]:
        if body.prompt is not None:
            latest_prompt = body.prompt
            conversation_text = body.prompt
        else:
            assert body.messages is not None
            latest_prompt = body.messages[-1].content
            conversation_text = " ".join(
                message.content for message in body.messages if message.role == "user"
            )
        prompt = latest_prompt.casefold()
        conversation = conversation_text.casefold()
        input_tokens = max(1, len(conversation_text.split()))
        if "ignore" in prompt and "delete" in prompt:
            decision = "refuse"
            answer = "I cannot expand scope or perform destructive admin actions."
            tool_calls: list[dict[str, object]] = []
        elif "which region" in prompt and "hefei" in conversation:
            decision = "answer"
            answer = "Your selected deployment region is Hefei."
            tool_calls = []
        elif "remember" in prompt and "hefei" in prompt:
            decision = "answer"
            answer = "I remembered that your deployment region is Hefei."
            tool_calls = []
        elif "schedule" in prompt or "calendar" in prompt:
            decision = "tool_call"
            answer = "I will use the calendar service to schedule the review."
            tool_calls = [
                {"name": "calendar.create", "arguments": {"region": "Hefei"}}
            ]
        elif "weather" in prompt:
            decision = "tool_call"
            answer = "I will use the weather service for Hefei."
            tool_calls = [
                {"name": "weather.lookup", "arguments": {"city": "Hefei"}}
            ]
        else:
            decision = "answer"
            answer = "Quality engineering uses evidence to manage release risk."
            tool_calls = []
        return {
            "decision": decision,
            "answer": answer,
            "tool_calls": tool_calls,
            "scope_expanded": False,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": len(answer.split()),
            },
        }

    return target


app = create_app()
