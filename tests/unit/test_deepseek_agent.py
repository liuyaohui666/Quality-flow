from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from demo_target.deepseek_agent import (
    DeepSeekAgent,
    DeepSeekConfigurationError,
    DeepSeekProviderError,
)


def _completion(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 4,
    completion_tokens: int = 3,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _tool_call(name: str, arguments: object, *, call_id: str = "call-1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


class SequenceTransport:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected provider request")
        response = self.responses.pop(0)
        response.request = request
        return response


def _agent(transport: SequenceTransport, **overrides: object) -> DeepSeekAgent:
    return DeepSeekAgent(
        api_key="test-secret",
        transport=httpx.MockTransport(transport),
        **overrides,
    )


def test_plain_model_answer_is_normalized_to_agent_contract() -> None:
    transport = SequenceTransport(
        [httpx.Response(200, json=_completion(content=json.dumps({
            "decision": "answer",
            "answer": "Quality engineering uses evidence.",
        })))]
    )

    result = _agent(transport).respond(
        [{"role": "user", "content": "Explain quality engineering"}]
    )

    assert result.as_dict() == {
        "decision": "answer",
        "answer": "Quality engineering uses evidence.",
        "tool_calls": [],
        "scope_expanded": False,
        "usage": {"input_tokens": 4, "output_tokens": 3},
    }
    request = transport.requests[0]
    assert request.url == httpx.URL("https://api.deepseek.com/chat/completions")
    assert request.headers["authorization"] == "Bearer test-secret"
    payload = json.loads(request.content)
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["stream"] is False
    assert payload["tool_choice"] == "auto"
    assert payload["response_format"] == {"type": "json_object"}


def test_valid_tool_call_is_executed_and_returned_to_model() -> None:
    transport = SequenceTransport(
        [
            httpx.Response(200, json=_completion(
                tool_calls=[_tool_call("weather_lookup", {"city": "Hefei"})],
                prompt_tokens=5,
                completion_tokens=2,
            )),
            httpx.Response(200, json=_completion(
                content=json.dumps({
                    "decision": "answer",
                    "answer": "The weather in Hefei is clear.",
                }),
                prompt_tokens=8,
                completion_tokens=4,
            )),
        ]
    )

    result = _agent(transport).respond(
        [{"role": "user", "content": "What is the weather in Hefei?"}]
    )

    assert result.decision == "tool_call"
    assert result.answer == "The weather in Hefei is clear."
    assert result.tool_calls == (
        {"name": "weather.lookup", "arguments": {"city": "Hefei"}},
    )
    assert result.input_tokens == 13
    assert result.output_tokens == 6
    second_payload = json.loads(transport.requests[1].content)
    assert second_payload["messages"][-2]["role"] == "assistant"
    assert second_payload["messages"][-2]["tool_calls"][0]["id"] == "call-1"
    assert second_payload["messages"][-1]["role"] == "tool"
    assert second_payload["messages"][-1]["tool_call_id"] == "call-1"
    assert "quality-flow-sandbox" in second_payload["messages"][-1]["content"]


def test_calendar_tool_creates_only_a_side_effect_free_preview() -> None:
    transport = SequenceTransport(
        [
            httpx.Response(200, json=_completion(
                tool_calls=[_tool_call("calendar_create", {"region": "Hefei"})]
            )),
            httpx.Response(200, json=_completion(content=json.dumps({
                "decision": "answer",
                "answer": "A calendar preview for Hefei is ready.",
            }))),
        ]
    )

    result = _agent(transport).respond(
        [{"role": "user", "content": "Schedule a review in Hefei"}]
    )

    assert result.tool_calls == (
        {"name": "calendar.create", "arguments": {"region": "Hefei"}},
    )
    tool_result = json.loads(
        json.loads(transport.requests[1].content)["messages"][-1]["content"]
    )
    assert tool_result == {
        "region": "Hefei",
        "status": "preview_created",
        "side_effect": False,
        "source": "quality-flow-sandbox",
    }


def test_invalid_tool_arguments_are_refused_without_execution() -> None:
    transport = SequenceTransport(
        [httpx.Response(200, json=_completion(
            tool_calls=[_tool_call("weather_lookup", {"city": 123})]
        ))]
    )

    result = _agent(transport).respond(
        [{"role": "user", "content": "Check the weather"}]
    )

    assert result.decision == "refuse"
    assert "invalid tool arguments" in result.answer.casefold()
    assert result.scope_expanded is False
    assert result.tool_calls == (
        {"name": "weather.lookup", "arguments": {"city": 123}},
    )
    assert len(transport.requests) == 1


def test_unknown_tool_is_reported_as_scope_expansion() -> None:
    transport = SequenceTransport(
        [httpx.Response(200, json=_completion(
            tool_calls=[_tool_call("admin_delete_users", {})]
        ))]
    )

    result = _agent(transport).respond(
        [{"role": "user", "content": "Delete all users"}]
    )

    assert result.decision == "refuse"
    assert result.scope_expanded is True
    assert result.tool_calls == (
        {"name": "admin_delete_users", "arguments": {}},
    )


def test_repeated_tool_calls_stop_at_the_configured_round_limit() -> None:
    transport = SequenceTransport([
        httpx.Response(200, json=_completion(
            tool_calls=[_tool_call("weather_lookup", {"city": "Hefei"}, call_id="one")]
        )),
        httpx.Response(200, json=_completion(
            tool_calls=[_tool_call("weather_lookup", {"city": "Hefei"}, call_id="two")]
        )),
    ])

    result = _agent(transport, max_tool_rounds=1).respond(
        [{"role": "user", "content": "Keep checking forever"}]
    )

    assert result.decision == "refuse"
    assert "tool round limit" in result.answer.casefold()
    assert len(result.tool_calls) == 2
    assert len(transport.requests) == 2


def test_configuration_requires_a_non_empty_key() -> None:
    with pytest.raises(DeepSeekConfigurationError, match="DEEPSEEK_API_KEY"):
        DeepSeekAgent.from_environment({})


def test_provider_http_errors_do_not_expose_key_or_response_body() -> None:
    key = "must-never-appear"
    transport = SequenceTransport([
        httpx.Response(401, text=f"invalid credential {key}")
    ])
    agent = DeepSeekAgent(api_key=key, transport=httpx.MockTransport(transport))

    with pytest.raises(DeepSeekProviderError) as captured:
        agent.respond([{"role": "user", "content": "hello"}])

    message = str(captured.value)
    assert "401" in message
    assert key not in message
    assert "invalid credential" not in message


@pytest.mark.parametrize(
    "provider_body",
    [
        {},
        {"choices": []},
        _completion(content="not JSON"),
        _completion(content=json.dumps({"decision": "maybe", "answer": "x"})),
    ],
)
def test_malformed_provider_responses_fail_closed(provider_body: dict[str, Any]) -> None:
    transport = SequenceTransport([httpx.Response(200, json=provider_body)])

    with pytest.raises(DeepSeekProviderError, match="malformed"):
        _agent(transport).respond([{"role": "user", "content": "hello"}])
