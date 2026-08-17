from pathlib import Path
import os
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


def test_repository_registry_points_to_both_demo_runner_suites(
    registry: SuiteRegistry,
) -> None:
    api_suite = registry.get("demo-api")
    load_suite = registry.get("demo-load")

    assert api_suite.runner_type == "pytest"
    assert api_suite.argv == (
        "python",
        "-m",
        "pytest",
        "demo_suites/api/test_target.py",
        "-q",
    )
    assert api_suite.resolve_parameters({"scenario": "ok"}) == {"scenario": "ok"}
    assert load_suite.runner_type == "locust"
    assert load_suite.argv[:5] == (
        "python",
        "-m",
        "locust",
        "-f",
        "demo_suites/load/locustfile.py",
    )
    assert load_suite.resolve_parameters({"scenario": "degraded"}) == {
        "scenario": "degraded"
    }


def test_repository_registry_registers_restful_booker_api(
    registry: SuiteRegistry,
) -> None:
    suite = registry.get("restful-booker-api")

    assert suite.runner_type == "pytest"
    assert suite.working_directory == (
        Path(__file__).resolve().parents[2] / "demo_suites" / "restful_booker"
    ).resolve()
    assert suite.argv == (
        "python",
        "-m",
        "pytest",
        "tests/test_booking_crud.py",
        "-q",
        "--strict-markers",
    )
    assert suite.timeout_seconds == 120
    assert suite.resolve_parameters({}) == {}
    with pytest.raises(InvalidSuiteParameter):
        suite.resolve_parameters({"base_url": "https://example.test"})
    assert suite.gate_policy.min_pass_rate == 1.0
    assert suite.gate_policy.max_failures == 0


def test_settings_rejects_symlinked_config_outside_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    config_link = project_root / "config"
    if os.name == "nt":
        junction = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(config_link), str(outside_root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode:
            pytest.skip(f"Could not create test junction: {junction.stderr}")
    else:
        config_link.symlink_to(outside_root, target_is_directory=True)
    monkeypatch.setenv("QUALITY_FLOW_SUITES_CONFIG", "config/suites.yaml")

    with pytest.raises(ValueError, match="project-relative"):
        Settings.from_environment(project_root)


def test_settings_uses_explicit_absolute_project_root_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "image-source"
    config = project_root / "config" / "suites.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("suites: {}\n", encoding="utf-8")
    monkeypatch.setenv("QUALITY_FLOW_PROJECT_ROOT", str(project_root))

    settings = Settings.from_environment(tmp_path / "site-packages")

    assert settings.project_root == project_root.resolve()
    assert settings.suites_config_path == config.resolve()


def test_settings_rejects_relative_explicit_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUALITY_FLOW_PROJECT_ROOT", "relative/source")

    with pytest.raises(ValueError, match="QUALITY_FLOW_PROJECT_ROOT"):
        Settings.from_environment(tmp_path)
