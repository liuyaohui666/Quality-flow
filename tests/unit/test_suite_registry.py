from pathlib import Path

import pytest

from quality_flow.suites.registry import (
    InvalidSuiteParameter,
    SuiteRegistry,
    UnknownSuiteError,
)


@pytest.fixture
def registry() -> SuiteRegistry:
    project_root = Path(__file__).resolve().parents[2]
    return SuiteRegistry.from_yaml(project_root / "config" / "suites.yaml", project_root)


def test_registry_rejects_unknown_suite(registry: SuiteRegistry) -> None:
    with pytest.raises(UnknownSuiteError):
        registry.get("arbitrary-command")


def test_registry_rejects_parameter_outside_allowlist(registry: SuiteRegistry) -> None:
    suite = registry.get("demo-api")

    with pytest.raises(InvalidSuiteParameter):
        suite.resolve_parameters({"scenario": "; rm -rf /"})
