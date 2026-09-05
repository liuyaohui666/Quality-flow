"""Strict, immutable definitions for structured Agent evaluations."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
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
_CASE_KEYS = {
    "id",
    "name",
    "prompt",
    "expect",
    "turns",
    "expected_tool_sequence",
    "max_total_tokens",
    "sample_count",
    "min_sample_pass_rate",
    "min_behavior_consistency_rate",
}
_TURN_KEYS = {"prompt", "expect"}
_EXPECT_KEYS = {
    "status",
    "decision",
    "answer_contains",
    "allowed_tools",
    "required_tools",
    "forbid_scope_expansion",
    "max_output_tokens",
    "tool_argument_schemas",
    "max_tool_calls",
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
    tool_argument_schemas: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    max_tool_calls: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "answer_contains", tuple(self.answer_contains))
        object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))
        object.__setattr__(self, "required_tools", frozenset(self.required_tools))
        object.__setattr__(
            self,
            "tool_argument_schemas",
            MappingProxyType(
                {
                    name: dict(schema)
                    for name, schema in self.tool_argument_schemas.items()
                }
            ),
        )


@dataclass(frozen=True)
class AgentEvalTurn:
    prompt: str
    expectation: AgentExpectation


@dataclass(frozen=True)
class AgentEvalCase:
    case_id: str
    name: str
    turns: tuple[AgentEvalTurn, ...]
    legacy_prompt: bool
    expected_tool_sequence: tuple[str, ...] = ()
    max_total_tokens: int | None = None
    sample_count: int = 1
    min_sample_pass_rate: float = 1.0
    min_behavior_consistency_rate: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "turns", tuple(self.turns))
        object.__setattr__(
            self, "expected_tool_sequence", tuple(self.expected_tool_sequence)
        )


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
        planned_turns = sum(
            case.sample_count * len(case.turns) for case in cases
        )
        if planned_turns > 500:
            raise AgentEvalDefinitionError(
                "Agent evaluation may schedule at most 500 HTTP turns"
            )
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
    has_legacy = "prompt" in case or "expect" in case
    has_turns = "turns" in case
    if has_legacy == has_turns:
        raise AgentEvalDefinitionError(
            f"{location} must define exactly one of prompt/expect or turns"
        )
    if has_legacy:
        turns = (
            AgentEvalTurn(
                prompt=_text(case.get("prompt"), f"{location}.prompt"),
                expectation=_parse_expectation(
                    case.get("expect"), f"{location}.expect"
                ),
            ),
        )
    else:
        raw_turns = case.get("turns")
        if not isinstance(raw_turns, list) or not 1 <= len(raw_turns) <= 20:
            raise AgentEvalDefinitionError(
                f"{location}.turns must contain between 1 and 20 items"
            )
        turns = tuple(
            _parse_turn(item, f"{location}.turns[{index}]")
            for index, item in enumerate(raw_turns)
        )
    expected_tool_sequence = _ordered_text_list(
        case.get("expected_tool_sequence", []),
        f"{location}.expected_tool_sequence",
    )
    max_total_tokens = case.get("max_total_tokens")
    if max_total_tokens is not None and (
        type(max_total_tokens) is not int
        or not 1 <= max_total_tokens <= 1_000_000
    ):
        raise AgentEvalDefinitionError(
            f"{location}.max_total_tokens must be a positive integer"
        )
    sample_count = case.get("sample_count", 1)
    if type(sample_count) is not int or not 1 <= sample_count <= 10:
        raise AgentEvalDefinitionError(
            f"{location}.sample_count must be between 1 and 10"
        )
    min_sample_pass_rate = _ratio(
        case.get("min_sample_pass_rate", 1.0),
        f"{location}.min_sample_pass_rate",
    )
    min_behavior_consistency_rate = _ratio(
        case.get("min_behavior_consistency_rate", 1.0),
        f"{location}.min_behavior_consistency_rate",
    )
    return AgentEvalCase(
        case_id=case_id,
        name=name,
        turns=turns,
        legacy_prompt=has_legacy,
        expected_tool_sequence=expected_tool_sequence,
        max_total_tokens=max_total_tokens,
        sample_count=sample_count,
        min_sample_pass_rate=min_sample_pass_rate,
        min_behavior_consistency_rate=min_behavior_consistency_rate,
    )


def _parse_turn(raw: Any, location: str) -> AgentEvalTurn:
    turn = _mapping(raw, location)
    _reject_unknown(turn, _TURN_KEYS, location)
    return AgentEvalTurn(
        prompt=_text(turn.get("prompt"), f"{location}.prompt"),
        expectation=_parse_expectation(turn.get("expect"), f"{location}.expect"),
    )


def _parse_expectation(raw: Any, location: str) -> AgentExpectation:
    expect = _mapping(raw, location)
    _reject_unknown(expect, _EXPECT_KEYS, location)
    status = expect.get("status", 200)
    if type(status) is not int or not 100 <= status <= 599:
        raise AgentEvalDefinitionError(f"{location}.status is invalid")
    decision = expect.get("decision")
    if decision not in _DECISIONS:
        raise AgentEvalDefinitionError(f"{location}.decision is invalid")
    answer_contains = _text_list(
        expect.get("answer_contains", []), f"{location}.answer_contains"
    )
    allowed_tools = frozenset(
        _text_list(expect.get("allowed_tools", []), f"{location}.allowed_tools")
    )
    required_tools = frozenset(
        _text_list(expect.get("required_tools", []), f"{location}.required_tools")
    )
    if not required_tools <= allowed_tools:
        raise AgentEvalDefinitionError(
            f"{location}.required_tools must be included in allowed_tools"
        )
    forbid_scope_expansion = expect.get("forbid_scope_expansion", True)
    if type(forbid_scope_expansion) is not bool:
        raise AgentEvalDefinitionError(
            f"{location}.forbid_scope_expansion must be boolean"
        )
    max_output_tokens = expect.get("max_output_tokens")
    if max_output_tokens is not None and (
        type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 1_000_000
    ):
        raise AgentEvalDefinitionError(
            f"{location}.max_output_tokens must be a positive integer"
        )
    tool_argument_schemas = _tool_argument_schemas(
        expect.get("tool_argument_schemas", {}),
        allowed_tools,
        f"{location}.tool_argument_schemas",
    )
    max_tool_calls = expect.get("max_tool_calls")
    if max_tool_calls is not None and (
        type(max_tool_calls) is not int or not 0 <= max_tool_calls <= 100
    ):
        raise AgentEvalDefinitionError(
            f"{location}.max_tool_calls must be between 0 and 100"
        )
    return AgentExpectation(
        status=status,
        decision=decision,
        answer_contains=answer_contains,
        allowed_tools=allowed_tools,
        required_tools=required_tools,
        forbid_scope_expansion=forbid_scope_expansion,
        max_output_tokens=max_output_tokens,
        tool_argument_schemas=tool_argument_schemas,
        max_tool_calls=max_tool_calls,
    )


def _tool_argument_schemas(
    value: Any,
    allowed_tools: frozenset[str],
    location: str,
) -> dict[str, dict[str, Any]]:
    schemas = _mapping(value, location)
    parsed: dict[str, dict[str, Any]] = {}
    for tool_name, raw_schema in schemas.items():
        if not tool_name.strip():
            raise AgentEvalDefinitionError(
                f"{location} tool names must be non-empty strings"
            )
        if tool_name not in allowed_tools:
            raise AgentEvalDefinitionError(
                f"{location} keys must be included in allowed_tools"
            )
        schema = _mapping(raw_schema, f"{location}.{tool_name}")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise AgentEvalDefinitionError(
                f"{location}.{tool_name} must be valid Draft 2020-12 JSON Schema"
            ) from error
        parsed[tool_name] = schema
    return parsed


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


def _ratio(value: Any, location: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise AgentEvalDefinitionError(f"{location} must be between 0 and 1")
    return float(value)


def _text_list(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise AgentEvalDefinitionError(f"{location} must be a list of strings")
    if len(value) != len(set(value)):
        raise AgentEvalDefinitionError(f"{location} must not contain duplicates")
    return tuple(value)


def _ordered_text_list(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise AgentEvalDefinitionError(f"{location} must be a list of strings")
    return tuple(value)
