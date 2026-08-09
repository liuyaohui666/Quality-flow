from pathlib import Path
import subprocess

import pytest

from quality_flow.infrastructure.config import Settings
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


def test_settings_rejects_symlinked_config_outside_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    config_link = project_root / "config"
    junction = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(config_link), str(outside_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode:
        pytest.skip(f"Could not create test junction: {junction.stderr}")
    monkeypatch.setenv("QUALITY_FLOW_SUITES_CONFIG", "config/suites.yaml")

    with pytest.raises(ValueError, match="project-relative"):
        Settings.from_environment(project_root)
