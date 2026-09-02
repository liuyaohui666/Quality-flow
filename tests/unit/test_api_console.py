from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from quality_flow.api.app import create_app
from quality_flow.suites.registry import GatePolicy, RetryPolicy, SuiteDefinition


def _suite() -> SuiteDefinition:
    return SuiteDefinition(
        suite_id="demo-api",
        runner_type="pytest",
        working_directory=Path.cwd(),
        argv=("python", "-m", "pytest"),
        timeout_seconds=30,
        allowed_parameters={"scenario": ("ok", "error", "slow")},
        gate_policy=GatePolicy(),
        retry_policy=RetryPolicy(),
        source_revision="main",
    )


def _client() -> TestClient:
    dependencies = SimpleNamespace(
        run_service=SimpleNamespace(),
        run_reader=SimpleNamespace(),
        readiness_check=lambda: None,
        suite_definitions=(_suite(),),
        artifact_store=None,
    )
    return TestClient(create_app(dependencies))


def test_suite_catalog_exposes_only_safe_creation_fields() -> None:
    response = _client().get("/api/v1/suites")

    assert response.status_code == 200
    assert response.json() == {
        "suites": [
            {
                "suite_id": "demo-api",
                "runner_type": "pytest",
                "allowed_parameters": {
                    "scenario": ["ok", "error", "slow"],
                },
            }
        ]
    }
    assert "argv" not in response.text
    assert "working_directory" not in response.text

