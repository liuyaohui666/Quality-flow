"""Strict, immutable definitions for declarative HTTP workflows."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_EXACT_TEMPLATE = re.compile(r"^\{\{\s*([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*)\s*\}\}$")
_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*)\s*\}\}")
_ROOT_KEYS = {
    "version",
    "name",
    "base_url",
    "request_timeout_seconds",
    "steps",
    "cleanup",
}
_STEP_KEYS = {"id", "name", "when_variables", "request", "expect", "capture"}
_REQUEST_KEYS = {"method", "path", "headers", "query", "json"}
_EXPECT_KEYS = {"status", "json_contains", "json_schema"}
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_NAMESPACES = frozenset({"request", "parameters", "env"})


class WorkflowDefinitionError(ValueError):
    """Raised when a trusted workflow definition violates its contract."""


class MissingTemplateVariable(WorkflowDefinitionError):
    """Raised when a workflow references data that is not available yet."""


@dataclass(frozen=True)
class WorkflowRequest:
    method: str
    path: str
    headers: Mapping[str, Any] = field(default_factory=dict)
    query: Mapping[str, Any] = field(default_factory=dict)
    json: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(deepcopy(dict(self.headers))))
        object.__setattr__(self, "query", MappingProxyType(deepcopy(dict(self.query))))
        object.__setattr__(self, "json", deepcopy(self.json))


@dataclass(frozen=True)
class WorkflowExpectation:
    status: int
    json_contains: Any | None = None
    json_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "json_contains", deepcopy(self.json_contains))
        object.__setattr__(
            self,
            "json_schema",
            (
                MappingProxyType(deepcopy(dict(self.json_schema)))
                if self.json_schema is not None
                else None
            ),
        )


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    name: str
    request: WorkflowRequest
    expect: WorkflowExpectation
    capture: Mapping[str, str] = field(default_factory=dict)
    when_variables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "capture", MappingProxyType(dict(self.capture)))
        object.__setattr__(self, "when_variables", tuple(self.when_variables))


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    base_url: str
    request_timeout_seconds: float
    steps: tuple[WorkflowStep, ...]
    cleanup: tuple[WorkflowStep, ...] = ()

    @classmethod
    def from_yaml(cls, path: Path) -> "WorkflowDefinition":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise WorkflowDefinitionError("workflow YAML is unreadable or malformed") from error
        root = _mapping(raw, "workflow")
        _reject_unknown(root, _ROOT_KEYS, "workflow")
        if root.get("version") != 1:
            raise WorkflowDefinitionError("workflow version must be 1")
        name = _non_empty_text(root.get("name"), "workflow name")
        base_url = _non_empty_text(root.get("base_url"), "workflow base_url")
        timeout = root.get("request_timeout_seconds", 10)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 60
        ):
            raise WorkflowDefinitionError(
                "request_timeout_seconds must be between 0 and 60"
            )
        steps = _parse_steps(root.get("steps"), "steps", allow_empty=False)
        cleanup = _parse_steps(root.get("cleanup", []), "cleanup", allow_empty=True)
        ids = [step.step_id for step in (*steps, *cleanup)]
        if len(ids) != len(set(ids)):
            raise WorkflowDefinitionError("workflow step ids must be unique")
        return cls(
            name=name,
            base_url=base_url,
            request_timeout_seconds=float(timeout),
            steps=steps,
            cleanup=cleanup,
        )


def render_template(value: Any, context: Mapping[str, Any]) -> Any:
    """Render a restricted template recursively without evaluating expressions."""
    if isinstance(value, Mapping):
        return {key: render_template(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_template(item, context) for item in value]
    if not isinstance(value, str):
        return deepcopy(value)

    exact = _EXACT_TEMPLATE.fullmatch(value)
    if exact:
        return deepcopy(_resolve_template_name(exact.group(1), context))

    matches = list(_TEMPLATE.finditer(value))
    if "{{" in value or "}}" in value:
        covered = _TEMPLATE.sub("", value)
        if "{{" in covered or "}}" in covered:
            raise WorkflowDefinitionError("template expressions use an unsupported form")
    rendered = value
    for match in matches:
        resolved = _resolve_template_name(match.group(1), context)
        if isinstance(resolved, (dict, list, tuple)):
            raise WorkflowDefinitionError(
                "structured template values must occupy the entire string"
            )
        rendered = rendered.replace(match.group(0), str(resolved))
    return rendered


def extract_json_path(value: Any, path: str) -> Any:
    """Read a mapping-only JSON path such as ``$.data.id``."""
    if not isinstance(path, str) or not path.startswith("$."):
        raise WorkflowDefinitionError("capture paths must start with $.")
    parts = path[2:].split(".")
    if not parts or any(not part for part in parts):
        raise WorkflowDefinitionError("capture path is empty or malformed")
    current = value
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            raise WorkflowDefinitionError(f"capture path is unavailable: {path}")
        current = current[part]
    return deepcopy(current)


def _resolve_template_name(name: str, context: Mapping[str, Any]) -> Any:
    parts = name.split(".")
    if parts[0] in _NAMESPACES:
        current: Any = context.get(parts[0], {})
        remaining = parts[1:]
    elif len(parts) == 1 and parts[0] in context:
        current = context[parts[0]]
        remaining = []
    else:
        raise WorkflowDefinitionError(f"template namespace is not allowed: {parts[0]}")
    for part in remaining:
        if not isinstance(current, Mapping) or part not in current:
            raise MissingTemplateVariable(f"template variable is unavailable: {name}")
        current = current[part]
    return current


def _parse_steps(raw: Any, location: str, *, allow_empty: bool) -> tuple[WorkflowStep, ...]:
    if not isinstance(raw, list) or (not raw and not allow_empty):
        raise WorkflowDefinitionError(f"workflow {location} must be a non-empty list")
    return tuple(_parse_step(item, f"{location}[{index}]") for index, item in enumerate(raw))


def _parse_step(raw: Any, location: str) -> WorkflowStep:
    step = _mapping(raw, location)
    _reject_unknown(step, _STEP_KEYS, location)
    step_id = _identifier(step.get("id"), f"{location}.id")
    name = _non_empty_text(step.get("name"), f"{location}.name")
    request_raw = _mapping(step.get("request"), f"{location}.request")
    _reject_unknown(request_raw, _REQUEST_KEYS, f"{location}.request")
    method = _non_empty_text(request_raw.get("method"), f"{location}.request.method").upper()
    if method not in _METHODS:
        raise WorkflowDefinitionError(f"{location} uses an unsupported HTTP method")
    path = _non_empty_text(request_raw.get("path"), f"{location}.request.path")
    headers = _string_key_mapping(request_raw.get("headers", {}), f"{location}.request.headers")
    query = _string_key_mapping(request_raw.get("query", {}), f"{location}.request.query")

    expect_raw = _mapping(step.get("expect"), f"{location}.expect")
    _reject_unknown(expect_raw, _EXPECT_KEYS, f"{location}.expect")
    status = expect_raw.get("status")
    if type(status) is not int or not 100 <= status <= 599:
        raise WorkflowDefinitionError(f"{location}.expect.status must be an HTTP status")
    schema = expect_raw.get("json_schema")
    if schema is not None and not isinstance(schema, Mapping):
        raise WorkflowDefinitionError(f"{location}.expect.json_schema must be a mapping")
    if schema is not None:
        try:
            Draft202012Validator.check_schema(dict(schema))
        except SchemaError as error:
            raise WorkflowDefinitionError(
                f"{location}.expect.json_schema is invalid: {error.message}"
            ) from error

    capture_raw = _string_key_mapping(step.get("capture", {}), f"{location}.capture")
    captures: dict[str, str] = {}
    for capture_name, capture_path in capture_raw.items():
        _identifier(capture_name, f"{location}.capture name")
        if not isinstance(capture_path, str) or not capture_path.startswith("$."):
            raise WorkflowDefinitionError(f"{location}.capture paths must start with $.")
        captures[capture_name] = capture_path

    when_raw = step.get("when_variables", [])
    if not isinstance(when_raw, list) or not all(isinstance(item, str) for item in when_raw):
        raise WorkflowDefinitionError(f"{location}.when_variables must be a list of names")
    when_variables = tuple(_identifier(item, f"{location}.when_variables") for item in when_raw)

    return WorkflowStep(
        step_id=step_id,
        name=name,
        request=WorkflowRequest(
            method=method,
            path=path,
            headers=headers,
            query=query,
            json=deepcopy(request_raw.get("json")),
        ),
        expect=WorkflowExpectation(
            status=status,
            json_contains=deepcopy(expect_raw.get("json_contains")),
            json_schema=dict(schema) if schema is not None else None,
        ),
        capture=captures,
        when_variables=when_variables,
    )


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise WorkflowDefinitionError(f"{location} must be a mapping")
    return dict(value)


def _string_key_mapping(value: Any, location: str) -> dict[str, Any]:
    return _mapping(value, location)


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise WorkflowDefinitionError(f"{location} has unknown fields: {', '.join(unknown)}")


def _non_empty_text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowDefinitionError(f"{location} must be a non-empty string")
    return value


def _identifier(value: Any, location: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise WorkflowDefinitionError(f"{location} must be a safe identifier")
    return value
