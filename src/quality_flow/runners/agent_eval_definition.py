"""Strict, immutable definitions for structured Agent evaluations."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any, Mapping

import yaml


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DECISIONS = frozenset({"answer", "tool_call", "refuse"})
_ROOT_KEYS = {
    "version",
    "name",
    "base_url",
    "path",
    "request_timeout_seconds",
    "cases",
}
_CASE_KEYS = {"id", "name", "prompt", "expect"}
_EXPECT_KEYS = {
    "status",
    "decision",
    "answer_contains",
    "allowed_tools",
    "required_tools",
    "forbid_scope_expansion",
    "max_output_tokens",
}


class AgentEvalDefinitionError(ValueError):
    """Raised when a trusted Agent evaluation definition is invalid."""


@dataclass(frozen=True)
class AgentExpectation:
    status: int
    decision: str
    answer_contains: tuple[str, ...] = ()
    allowed_tools: frozenset[str] = frozenset()
    required_tools: frozenset[str] = frozenset()
    forbid_scope_expansion: bool = True
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "answer_contains", tuple(self.answer_contains))
        object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))
        object.__setattr__(self, "required_tools", frozenset(self.required_tools))


@dataclass(frozen=True)
class AgentEvalCase:
    case_id: str
    name: str
    prompt: str
    expectation: AgentExpectation


@dataclass(frozen=True)
class AgentEvalDefinition:
    name: str
    base_url: str
    path: str
    request_timeout_seconds: float
    cases: tuple[AgentEvalCase, ...]

    @classmethod
    def from_yaml(cls, path: Path) -> "AgentEvalDefinition":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise AgentEvalDefinitionError(
                "Agent evaluation YAML is unreadable or malformed"
            ) from error
        root = _mapping(raw, "agent evaluation")
        _reject_unknown(root, _ROOT_KEYS, "agent evaluation")
        if root.get("version") != 1:
            raise AgentEvalDefinitionError("Agent evaluation version must be 1")
        name = _text(root.get("name"), "agent evaluation name")
        base_url = _text(root.get("base_url"), "agent evaluation base_url")
        endpoint_path = _text(root.get("path"), "agent evaluation path")
        if not endpoint_path.startswith("/") or endpoint_path.startswith("//"):
            raise AgentEvalDefinitionError(
                "agent evaluation path must be relative and start with /"
            )
        timeout = root.get("request_timeout_seconds", 10)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 60
        ):
            raise AgentEvalDefinitionError(
                "request_timeout_seconds must be between 0 and 60"
            )
        raw_cases = root.get("cases")
        if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= 100:
            raise AgentEvalDefinitionError("cases must contain between 1 and 100 items")
        cases = tuple(
            _parse_case(item, f"cases[{index}]")
            for index, item in enumerate(raw_cases)
        )
        if len({case.case_id for case in cases}) != len(cases):
            raise AgentEvalDefinitionError("Agent evaluation case ids must be unique")
        return cls(
            name=name,
            base_url=base_url,
            path=endpoint_path,
            request_timeout_seconds=float(timeout),
            cases=cases,
        )


def _parse_case(raw: Any, location: str) -> AgentEvalCase:
    case = _mapping(raw, location)
    _reject_unknown(case, _CASE_KEYS, location)
    case_id = _identifier(case.get("id"), f"{location}.id")
    name = _text(case.get("name"), f"{location}.name")
    prompt = _text(case.get("prompt"), f"{location}.prompt")
    expect = _mapping(case.get("expect"), f"{location}.expect")
    _reject_unknown(expect, _EXPECT_KEYS, f"{location}.expect")
    status = expect.get("status", 200)
    if type(status) is not int or not 100 <= status <= 599:
        raise AgentEvalDefinitionError(f"{location}.expect.status is invalid")
    decision = expect.get("decision")
    if decision not in _DECISIONS:
        raise AgentEvalDefinitionError(f"{location}.expect.decision is invalid")
    answer_contains = _text_list(
        expect.get("answer_contains", []), f"{location}.expect.answer_contains"
    )
    allowed_tools = frozenset(
        _text_list(expect.get("allowed_tools", []), f"{location}.expect.allowed_tools")
    )
    required_tools = frozenset(
        _text_list(
            expect.get("required_tools", []), f"{location}.expect.required_tools"
        )
    )
    if not required_tools <= allowed_tools:
        raise AgentEvalDefinitionError(
            f"{location}.expect.required_tools must be included in allowed_tools"
        )
    forbid_scope_expansion = expect.get("forbid_scope_expansion", True)
    if type(forbid_scope_expansion) is not bool:
        raise AgentEvalDefinitionError(
            f"{location}.expect.forbid_scope_expansion must be boolean"
        )
    max_output_tokens = expect.get("max_output_tokens")
    if max_output_tokens is not None and (
        type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 1_000_000
    ):
        raise AgentEvalDefinitionError(
            f"{location}.expect.max_output_tokens must be a positive integer"
        )
    return AgentEvalCase(
        case_id=case_id,
        name=name,
        prompt=prompt,
        expectation=AgentExpectation(
            status=status,
            decision=decision,
            answer_contains=answer_contains,
            allowed_tools=allowed_tools,
            required_tools=required_tools,
            forbid_scope_expansion=forbid_scope_expansion,
            max_output_tokens=max_output_tokens,
        ),
    )


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise AgentEvalDefinitionError(f"{location} must be a mapping")
    return dict(value)


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str], location: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AgentEvalDefinitionError(
            f"{location} has unknown fields: {', '.join(unknown)}"
        )


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentEvalDefinitionError(f"{location} must be a non-empty string")
    return value


def _identifier(value: Any, location: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AgentEvalDefinitionError(f"{location} must be a safe identifier")
    return value


def _text_list(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise AgentEvalDefinitionError(f"{location} must be a list of strings")
    if len(value) != len(set(value)):
        raise AgentEvalDefinitionError(f"{location} must not contain duplicates")
    return tuple(value)
