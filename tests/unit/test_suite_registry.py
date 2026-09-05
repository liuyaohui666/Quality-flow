from dataclasses import FrozenInstanceError
from pathlib import Path
import os
import subprocess

import pytest

from quality_flow.infrastructure.config import Settings
from quality_flow.suites.registry import (
    InvalidSuiteParameter,
    InvalidSuiteRequestBody,
    SuiteRegistryError,
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


def test_registry_accepts_only_registered_workflow_definitions(tmp_path: Path) -> None:
    definition = tmp_path / "resource.yaml"
    definition.write_text("version: 1\n", encoding="utf-8")
    config = tmp_path / "suites.yaml"
    config.write_text(
        """
suites:
  resource-workflow:
    test_type: api
    runner_type: workflow
    working_directory: .
    argv: [workflow, resource.yaml]
    timeout_seconds: 10
    allowed_parameters: {}
    gate_policy:
      min_pass_rate: 1.0
      max_failures: 0
    source_revision: test
""",
        encoding="utf-8",
    )

    suite = SuiteRegistry.from_yaml(config, tmp_path).get("resource-workflow")

    assert suite.runner_type == "workflow"
    assert suite.argv == ("workflow", "resource.yaml")


def test_registry_accepts_registered_agent_evaluation_definitions(
    tmp_path: Path,
) -> None:
    suite_root = tmp_path / "agent"
    suite_root.mkdir()
    (suite_root / "evaluation.yaml").write_text("version: 1\n", encoding="utf-8")
    config = tmp_path / "suites.yaml"
    config.write_text(
        """
suites:
  agent-safety:
    test_type: agent
    runner_type: agent_eval
    working_directory: agent
    argv: [agent_eval, evaluation.yaml]
    timeout_seconds: 30
    allowed_parameters: {}
    source_revision: main
    gate_policy:
      min_pass_rate: 1.0
      max_failures: 0
""",
        encoding="utf-8",
    )

    suite = SuiteRegistry.from_yaml(config, tmp_path).get("agent-safety")

    assert suite.runner_type == "agent_eval"
    assert suite.test_type == "agent"
    assert suite.argv == ("agent_eval", "evaluation.yaml")


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


def test_repository_registry_registers_local_dependent_workflow(
    registry: SuiteRegistry,
) -> None:
    suite = registry.get("demo-workflow")

    assert suite.runner_type == "workflow"
    assert suite.test_type == "api"
    assert suite.argv == ("workflow", "resource_lifecycle.yaml")
    assert suite.working_directory.name == "workflow"
    assert suite.request_body is not None and suite.request_body.required is True
    assert suite.resolve_request_body(
        {"name": "Ada", "updated_name": "Grace"}
    ) == {"name": "Ada", "updated_name": "Grace"}


def test_repository_registry_registers_local_agent_evaluation(
    registry: SuiteRegistry,
) -> None:
    suite = registry.get("demo-agent-eval")

    assert suite.runner_type == "agent_eval"
    assert suite.test_type == "agent"
    assert suite.argv == ("agent_eval", "safety.yaml")
    assert suite.working_directory.name == "agent_eval"


def test_restful_booker_request_body_contract_accepts_valid_business_json(
    registry: SuiteRegistry,
) -> None:
    suite = registry.get("restful-booker-api")
    payload = {
        "firstname": "Lin",
        "lastname": "Hui",
        "totalprice": 268,
        "depositpaid": True,
        "bookingdates": {
            "checkin": "2027-09-01",
            "checkout": "2027-09-05",
        },
        "additionalneeds": "Breakfast",
    }

    assert suite.test_type == "api"
    assert suite.resolve_request_body(payload) == payload
    assert suite.resolve_request_body(payload) is not payload


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"firstname": "Lin"}, "request_body"),
        (
            {
                "firstname": "Lin",
                "lastname": "Hui",
                "totalprice": "268",
                "depositpaid": True,
                "bookingdates": {
                    "checkin": "2027-09-01",
                    "checkout": "2027-09-05",
                },
                "additionalneeds": "Breakfast",
            },
            "totalprice",
        ),
        (
            {
                "firstname": "Lin",
                "lastname": "Hui",
                "totalprice": 268,
                "depositpaid": True,
                "bookingdates": {
                    "checkin": "2027-09-01",
                    "checkout": "2027-09-05",
                },
                "additionalneeds": "Breakfast",
                "command": "rm -rf /",
            },
            "command",
        ),
    ],
)
def test_restful_booker_request_body_contract_rejects_invalid_business_json(
    registry: SuiteRegistry,
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(InvalidSuiteRequestBody, match=message):
        registry.get("restful-booker-api").resolve_request_body(payload)


def test_suite_without_request_body_contract_rejects_a_body(
    registry: SuiteRegistry,
) -> None:
    suite = registry.get("demo-api")

    assert suite.test_type == "api"
    assert suite.resolve_request_body(None) is None
    with pytest.raises(InvalidSuiteRequestBody, match="does not accept"):
        suite.resolve_request_body({"unexpected": True})


def test_registry_parses_conservative_retry_policy(registry: SuiteRegistry) -> None:
    policy = registry.get("demo-api").retry_policy

    assert policy.max_attempts == 2
    assert policy.retry_on == frozenset({"worker_lost"})
    assert registry.get("restful-booker-api").retry_policy.max_attempts == 1
    with pytest.raises(FrozenInstanceError):
        policy.max_attempts = 1


@pytest.mark.parametrize(
    "policy_yaml",
    [
        "max_attempts: 0\nretry_on: [worker_lost]",
        "max_attempts: true\nretry_on: [worker_lost]",
        "max_attempts: 3\nretry_on: [worker_lost]",
        "max_attempts: 2\nretry_on: [timeout]",
    ],
)
def test_registry_rejects_invalid_retry_policy(
    tmp_path: Path, policy_yaml: str
) -> None:
    config = tmp_path / "suites.yaml"
    config.write_text(
        "suites:\n  demo:\n    runner_type: pytest\n"
        "    working_directory: .\n    argv: [python]\n"
        "    timeout_seconds: 1\n    source_revision: test\n"
        f"    retry_policy:\n      {policy_yaml.replace(chr(10), chr(10) + '      ')}\n",
        encoding="utf-8",
    )
    with pytest.raises(SuiteRegistryError, match="retry_policy"):
        SuiteRegistry.from_yaml(config, tmp_path)


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
