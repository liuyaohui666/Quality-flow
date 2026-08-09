"""Validated, immutable definitions of executable test suites."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

import yaml


class SuiteRegistryError(ValueError):
    """Base class for invalid suite registry data."""


class UnknownSuiteError(SuiteRegistryError):
    """Raised when a requested suite id is not registered."""


class InvalidSuiteParameter(SuiteRegistryError):
    """Raised when a suite parameter is not explicitly allowlisted."""


@dataclass(frozen=True)
class GatePolicy:
    min_pass_rate: float = 1.0
    max_failures: int = 0
    max_error_rate: float | None = None
    max_p95_ms: float | None = None
    min_requests: int | None = None


@dataclass(frozen=True)
class SuiteDefinition:
    suite_id: str
    runner_type: Literal["pytest", "locust"]
    working_directory: Path
    argv: tuple[str, ...]
    timeout_seconds: int
    allowed_parameters: Mapping[str, tuple[str, ...]]
    gate_policy: GatePolicy
    source_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "working_directory", self.working_directory.resolve())
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(
            self,
            "allowed_parameters",
            MappingProxyType(
                {name: tuple(values) for name, values in self.allowed_parameters.items()}
            ),
        )

    def resolve_parameters(self, supplied: Mapping[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for name, value in supplied.items():
            if name not in self.allowed_parameters:
                raise InvalidSuiteParameter(f"Unknown parameter: {name}")
            if value not in self.allowed_parameters[name]:
                raise InvalidSuiteParameter(
                    f"Value {value!r} is not allowlisted for parameter {name!r}"
                )
            resolved[name] = value
        return resolved


@dataclass(frozen=True)
class SuiteRegistry:
    _suites: Mapping[str, SuiteDefinition] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_suites", MappingProxyType(dict(self._suites)))

    def __len__(self) -> int:
        return len(self._suites)

    @classmethod
    def from_yaml(cls, path: Path, project_root: Path) -> "SuiteRegistry":
        root = project_root.resolve()
        with path.open(encoding="utf-8") as registry_file:
            data = yaml.safe_load(registry_file)

        if not isinstance(data, dict):
            raise SuiteRegistryError("Suite registry must be a mapping")
        raw_suites = data.get("suites")
        if not isinstance(raw_suites, dict):
            raise SuiteRegistryError("'suites' must be a mapping keyed by suite id")

        suites = {
            suite_id: cls._parse_suite(suite_id, raw_suite, root)
            for suite_id, raw_suite in raw_suites.items()
        }
        return cls(suites)

    @staticmethod
    def _parse_suite(
        suite_id: object, raw_suite: object, project_root: Path
    ) -> SuiteDefinition:
        if not isinstance(suite_id, str) or not suite_id:
            raise SuiteRegistryError("Suite ids must be non-empty strings")
        if not isinstance(raw_suite, dict):
            raise SuiteRegistryError(f"Suite {suite_id!r} must be a mapping")

        runner_type = raw_suite.get("runner_type")
        if runner_type not in ("pytest", "locust"):
            raise SuiteRegistryError(f"Suite {suite_id!r} has an invalid runner type")

        working_directory = SuiteRegistry._resolve_working_directory(
            raw_suite.get("working_directory"), project_root
        )
        argv = SuiteRegistry._parse_argv(raw_suite.get("argv"), suite_id)
        allowed_parameters = SuiteRegistry._parse_allowed_parameters(
            raw_suite.get("allowed_parameters", {}), suite_id
        )
        timeout_seconds = raw_suite.get("timeout_seconds")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise SuiteRegistryError(f"Suite {suite_id!r} needs a positive timeout_seconds")

        source_revision = raw_suite.get("source_revision")
        if not isinstance(source_revision, str) or not source_revision:
            raise SuiteRegistryError(f"Suite {suite_id!r} needs a source_revision")

        gate_data = raw_suite.get("gate_policy", {})
        if not isinstance(gate_data, dict):
            raise SuiteRegistryError(f"Suite {suite_id!r} gate_policy must be a mapping")
        try:
            gate_policy = GatePolicy(**gate_data)
        except TypeError as error:
            raise SuiteRegistryError(f"Suite {suite_id!r} has an invalid gate_policy") from error

        return SuiteDefinition(
            suite_id=suite_id,
            runner_type=runner_type,
            working_directory=working_directory,
            argv=argv,
            timeout_seconds=timeout_seconds,
            allowed_parameters=allowed_parameters,
            gate_policy=gate_policy,
            source_revision=source_revision,
        )

    @staticmethod
    def _resolve_working_directory(raw_path: object, project_root: Path) -> Path:
        if not isinstance(raw_path, str) or not raw_path:
            raise SuiteRegistryError("working_directory must be a non-empty relative path")
        candidate = Path(raw_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SuiteRegistryError("working_directory must not escape the project root")
        resolved = (project_root / candidate).resolve()
        try:
            resolved.relative_to(project_root)
        except ValueError as error:
            raise SuiteRegistryError("working_directory must stay inside the project root") from error
        return resolved

    @staticmethod
    def _parse_argv(raw_argv: object, suite_id: str) -> tuple[str, ...]:
        if isinstance(raw_argv, str) or not isinstance(raw_argv, list) or not raw_argv:
            raise SuiteRegistryError(f"Suite {suite_id!r} argv must be a non-empty argument list")
        if not all(isinstance(argument, str) and argument for argument in raw_argv):
            raise SuiteRegistryError(f"Suite {suite_id!r} argv entries must be non-empty strings")
        return tuple(raw_argv)

    @staticmethod
    def _parse_allowed_parameters(
        raw_parameters: object, suite_id: str
    ) -> dict[str, tuple[str, ...]]:
        if not isinstance(raw_parameters, dict):
            raise SuiteRegistryError(
                f"Suite {suite_id!r} allowed_parameters must be a mapping"
            )
        parsed: dict[str, tuple[str, ...]] = {}
        for name, values in raw_parameters.items():
            if not isinstance(name, str) or not name:
                raise SuiteRegistryError("Parameter names must be non-empty strings")
            if isinstance(values, str) or not isinstance(values, list) or not values:
                raise SuiteRegistryError(
                    f"Parameter {name!r} needs a non-empty explicit allowlist"
                )
            if not all(isinstance(value, str) for value in values):
                raise SuiteRegistryError(
                    f"Parameter {name!r} allowlist values must be strings"
                )
            parsed[name] = tuple(values)
        return parsed

    def get(self, suite_id: str) -> SuiteDefinition:
        try:
            return self._suites[suite_id]
        except KeyError as error:
            raise UnknownSuiteError(f"Unknown suite: {suite_id}") from error
