from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from quality_flow.api.app import create_app
from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
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


def _run(
    *,
    run_id: UUID | None = None,
    created_at: datetime | None = None,
    status: RunStatus = RunStatus.COMPLETED,
) -> SimpleNamespace:
    created = created_at or datetime.now(UTC)
    attempt_id = uuid4()
    return SimpleNamespace(
        run_id=run_id or uuid4(),
        suite_id="demo-api",
        status=status,
        outcome=RunOutcome.PASSED,
        created_at=created,
        updated_at=created,
        started_at=created,
        finished_at=created + timedelta(seconds=1),
        attempts=[
            SimpleNamespace(
                attempt_id=attempt_id,
                attempt_no=1,
                status=AttemptStatus.PASSED,
                created_at=created,
                started_at=created,
                finished_at=created + timedelta(seconds=1),
                exit_code=0,
            )
        ],
        events=[],
        case_results=[
            SimpleNamespace(
                case_result_id=uuid4(),
                attempt_id=attempt_id,
                node_id="tests/test_target.py::test_target",
                status="passed",
                duration_ms=12.5,
                message=None,
                details={"private": "hidden"},
                created_at=created,
            )
        ],
        metrics=[],
        gates=[],
        artifacts=[],
    )


class ConsoleRunReader:
    def __init__(self, runs: list[SimpleNamespace]) -> None:
        self.runs = runs
        self.list_call: tuple[int, RunStatus | None, str | None] | None = None

    def list_runs(
        self,
        limit: int,
        status: RunStatus | None,
        suite_id: str | None,
    ) -> tuple[SimpleNamespace, ...]:
        self.list_call = (limit, status, suite_id)
        return tuple(self.runs)

    def get_run(self, run_id: UUID) -> SimpleNamespace | None:
        return next((run for run in self.runs if run.run_id == run_id), None)


def _console_client(reader: ConsoleRunReader) -> TestClient:
    dependencies = SimpleNamespace(
        run_service=SimpleNamespace(),
        run_reader=reader,
        readiness_check=lambda: None,
        suite_definitions=(_suite(),),
        artifact_store=None,
    )
    return TestClient(create_app(dependencies))


def test_run_history_uses_bounded_filters_and_returns_summaries() -> None:
    run = _run()
    reader = ConsoleRunReader([run])

    response = _console_client(reader).get(
        "/api/v1/runs?status=completed&suite_id=demo-api&limit=20"
    )

    assert response.status_code == 200
    assert reader.list_call == (20, RunStatus.COMPLETED, "demo-api")
    assert response.json()["runs"] == [
        {
            "run_id": str(run.run_id),
            "suite_id": "demo-api",
            "status": "completed",
            "outcome": "passed",
            "created_at": run.created_at.isoformat().replace("+00:00", "Z"),
            "started_at": run.started_at.isoformat().replace("+00:00", "Z"),
            "finished_at": run.finished_at.isoformat().replace("+00:00", "Z"),
            "latest_attempt_no": 1,
        }
    ]


def test_run_history_rejects_unbounded_limit_and_unknown_status() -> None:
    client = _console_client(ConsoleRunReader([]))

    assert client.get("/api/v1/runs?limit=101").status_code == 422
    assert client.get("/api/v1/runs?status=missing").status_code == 422


def test_cases_expose_latest_attempt_without_arbitrary_details() -> None:
    run = _run()

    response = _console_client(ConsoleRunReader([run])).get(
        f"/api/v1/runs/{run.run_id}/cases"
    )

    assert response.status_code == 200
    assert response.json()["cases"] == [
        {
            "case_result_id": str(run.case_results[0].case_result_id),
            "attempt_id": str(run.case_results[0].attempt_id),
            "node_id": "tests/test_target.py::test_target",
            "status": "passed",
            "duration_ms": 12.5,
            "message": None,
            "created_at": run.created_at.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert "private" not in response.text
