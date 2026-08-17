from __future__ import annotations

import ast
from pathlib import Path
import shutil

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.base import ExecutionSpec
from quality_flow.runners.pytest_runner import PytestRunner
from quality_flow.suites.registry import SuiteRegistry


AUTH_SECRET_SENTINELS = (
    "raw-token-sentinel",
    "raw-password-sentinel",
    "raw-authorization-sentinel",
    "raw-cookie-sentinel",
    "raw-set-cookie-sentinel",
)


def test_live_delete_scenario_owns_cleanup_instead_of_double_deleting() -> None:
    project_root = Path(__file__).resolve().parents[2]
    crud_test_path = (
        project_root
        / "demo_suites"
        / "restful_booker"
        / "tests"
        / "test_booking_crud.py"
    )
    module = ast.parse(crud_test_path.read_text(encoding="utf-8"))
    delete_test = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_delete_booking_makes_resource_unavailable"
    )
    fixture_names = {argument.arg for argument in delete_test.args.args}

    assert "created_booking" not in fixture_names
    assert {"booking_client", "booking_data", "auth_token"} <= fixture_names


def test_pytest_runner_executes_restful_booker_offline_unit_suite(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    source_suite = project_root / "demo_suites" / "restful_booker"
    workspace = tmp_path / "workspace"
    shutil.copytree(
        source_suite,
        workspace,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "logs"),
    )
    registry = SuiteRegistry.from_yaml(
        project_root / "config" / "suites.yaml", project_root
    )
    suite = registry.get("restful-booker-api")
    spec = ExecutionSpec(
        argv=(
            "python",
            "-m",
            "pytest",
            "tests/test_booking_client.py",
            "tests/test_assertions.py",
            "tests/test_config.py",
            "tests/test_logger.py",
            "-q",
            "--strict-markers",
        ),
        timeout_seconds=suite.timeout_seconds,
        allowed_workspace_root=tmp_path,
        parameters=suite.resolve_parameters({}),
        gate_policy=suite.gate_policy,
    )

    outcome = PytestRunner(staging_root=tmp_path / "staging").run(
        spec, workspace, lambda: None
    )

    assert outcome.attempt_status is AttemptStatus.PASSED
    assert outcome.case_summary is not None
    assert outcome.case_summary.total >= 8
    assert outcome.case_summary.failed == 0
    assert outcome.case_summary.errors == 0
    assert outcome.gate_result is not None and outcome.gate_result.passed
    assert {item.artifact_type for item in outcome.artifacts} == {
        "stdout",
        "stderr",
        "junit_xml",
    }


def test_anomalous_auth_failure_never_persists_raw_secrets(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    source_suite = project_root / "demo_suites" / "restful_booker"
    workspace = tmp_path / "workspace"
    shutil.copytree(
        source_suite,
        workspace,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "logs"),
    )
    (workspace / "tests" / "test_auth_failure_evidence.py").write_text(
        '''import pytest


class FakeAuthResponse:
    status_code = 418
    text = (
        '{"token": "raw-token-sentinel", '
        '"password": "raw-password-sentinel", '
        '"authorization": "raw-authorization-sentinel", '
        '"cookie": "raw-cookie-sentinel", '
        '"set-cookie": "raw-set-cookie-sentinel"}'
    )

    def json(self):
        return {
            "token": "raw-token-sentinel",
            "password": "raw-password-sentinel",
            "authorization": "raw-authorization-sentinel",
            "cookie": "raw-cookie-sentinel",
            "set-cookie": "raw-set-cookie-sentinel",
        }


class FakeBookingClient:
    def create_token(self, credentials):
        return FakeAuthResponse()


@pytest.fixture(scope="session")
def booking_client():
    return FakeBookingClient()


@pytest.fixture(scope="session")
def settings():
    return {"auth": {"username": "admin", "password": "raw-password-sentinel"}}


def test_authentication(auth_token):
    raise AssertionError("auth fixture should fail before the test body")
''',
        encoding="utf-8",
    )
    registry = SuiteRegistry.from_yaml(
        project_root / "config" / "suites.yaml", project_root
    )
    suite = registry.get("restful-booker-api")
    spec = ExecutionSpec(
        argv=(
            "python",
            "-m",
            "pytest",
            "tests/test_auth_failure_evidence.py",
            "-q",
            "--strict-markers",
        ),
        timeout_seconds=suite.timeout_seconds,
        allowed_workspace_root=tmp_path,
        parameters=suite.resolve_parameters({}),
        gate_policy=suite.gate_policy,
    )

    outcome = PytestRunner(staging_root=tmp_path / "staging").run(
        spec, workspace, lambda: None
    )

    assert outcome.attempt_status is AttemptStatus.TEST_FAILED
    retained_evidence = "\n".join(
        artifact.source_path.read_text(encoding="utf-8", errors="replace")
        for artifact in outcome.artifacts
    )
    retained_evidence += "\n" + "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in workspace.rglob("*.log")
    )
    for sentinel in AUTH_SECRET_SENTINELS:
        assert sentinel not in retained_evidence


def test_failed_booking_cleanup_fails_without_persisting_secrets(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    source_suite = project_root / "demo_suites" / "restful_booker"
    workspace = tmp_path / "workspace"
    shutil.copytree(
        source_suite,
        workspace,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "logs"),
    )
    (workspace / "tests" / "test_cleanup_failure_evidence.py").write_text(
        '''import pytest


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeBookingClient:
    def create_booking(self, payload):
        return FakeResponse(200, {"bookingid": 7})

    def delete_booking(self, booking_id, token):
        return FakeResponse(
            500,
            {
                "token": "raw-token-sentinel",
                "password": "raw-password-sentinel",
                "authorization": "raw-authorization-sentinel",
                "cookie": "raw-cookie-sentinel",
                "set-cookie": "raw-set-cookie-sentinel",
            },
        )


@pytest.fixture
def booking_client():
    return FakeBookingClient()


@pytest.fixture
def booking_data():
    return {"valid_booking": {"firstname": "Ada"}}


@pytest.fixture
def auth_token():
    return "raw-token-sentinel"


def test_booking_use(created_booking):
    assert created_booking[0] == 7
''',
        encoding="utf-8",
    )
    registry = SuiteRegistry.from_yaml(
        project_root / "config" / "suites.yaml", project_root
    )
    suite = registry.get("restful-booker-api")
    spec = ExecutionSpec(
        argv=(
            "python",
            "-m",
            "pytest",
            "tests/test_cleanup_failure_evidence.py",
            "-q",
            "--strict-markers",
        ),
        timeout_seconds=suite.timeout_seconds,
        allowed_workspace_root=tmp_path,
        parameters=suite.resolve_parameters({}),
        gate_policy=suite.gate_policy,
    )

    outcome = PytestRunner(staging_root=tmp_path / "staging").run(
        spec, workspace, lambda: None
    )

    assert outcome.attempt_status is not AttemptStatus.PASSED
    assert outcome.exit_code == 1
    retained_evidence = "\n".join(
        artifact.source_path.read_text(encoding="utf-8", errors="replace")
        for artifact in outcome.artifacts
    )
    retained_evidence += "\n" + "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (tmp_path / "staging").rglob("*")
        if path.is_file()
    )
    retained_evidence += "\n" + "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in workspace.rglob("*.log")
    )
    assert "Expected status 201, got 500" in retained_evidence
    for sentinel in AUTH_SECRET_SENTINELS:
        assert sentinel not in retained_evidence
