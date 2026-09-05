from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from quality_flow.api.app import create_app
from quality_flow.domain.enums import AttemptStatus, RunOutcome, RunStatus
from quality_flow.suites.registry import (
    GatePolicy,
    RequestBodyDefinition,
    RetryPolicy,
    SuiteDefinition,
)


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
        test_type="api",
        request_body=RequestBodyDefinition(
            required=False,
            schema={
                "type": "object",
                "properties": {"firstname": {"type": "string"}},
                "required": ["firstname"],
                "additionalProperties": False,
            },
            example={"firstname": "Ada"},
        ),
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
                "test_type": "api",
                "allowed_parameters": {
                    "scenario": ["ok", "error", "slow"],
                },
                "request_body": {
                    "required": False,
                    "schema": {
                        "type": "object",
                        "properties": {"firstname": {"type": "string"}},
                        "required": ["firstname"],
                        "additionalProperties": False,
                    },
                    "example": {"firstname": "Ada"},
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


def _console_client(
    reader: ConsoleRunReader, artifact_store: object | None = None
) -> TestClient:
    dependencies = SimpleNamespace(
        run_service=SimpleNamespace(),
        run_reader=reader,
        readiness_check=lambda: None,
        suite_definitions=(_suite(),),
        artifact_store=artifact_store,
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


class FakeArtifactStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.resolved_uris: list[str] = []

    def resolve(self, uri: str) -> Path:
        self.resolved_uris.append(uri)
        return self.path


def test_artifact_content_uses_database_owned_uri(tmp_path: Path) -> None:
    run = _run()
    artifact_id = uuid4()
    artifact_path = tmp_path / "captured.log"
    artifact_path.write_bytes(b"captured output\n")
    run.artifacts = [
        SimpleNamespace(
            artifact_id=artifact_id,
            attempt_id=run.attempts[0].attempt_id,
            artifact_type="stdout",
            uri=f"runs/{run.run_id}/{run.attempts[0].attempt_id}/{uuid4().hex}",
            artifact_metadata={"mime_type": "text/plain", "size_bytes": 16},
        )
    ]
    store = FakeArtifactStore(artifact_path)
    client = _console_client(ConsoleRunReader([run]), store)

    inline = client.get(
        f"/api/v1/runs/{run.run_id}/artifacts/{artifact_id}/content"
    )
    download = client.get(
        f"/api/v1/runs/{run.run_id}/artifacts/{artifact_id}/content?download=true"
    )

    assert inline.status_code == download.status_code == 200
    assert inline.content == download.content == b"captured output\n"
    assert "inline" in inline.headers["content-disposition"]
    assert "attachment" in download.headers["content-disposition"]
    assert store.resolved_uris == [run.artifacts[0].uri, run.artifacts[0].uri]


def test_workflow_report_download_has_descriptive_filename(tmp_path: Path) -> None:
    run = _run()
    artifact_id = uuid4()
    artifact_path = tmp_path / "opaque-artifact"
    artifact_path.write_text('{"workflow_id":"resource-lifecycle"}', encoding="utf-8")
    run.artifacts = [
        SimpleNamespace(
            artifact_id=artifact_id,
            attempt_id=run.attempts[0].attempt_id,
            artifact_type="workflow_report",
            uri=f"runs/{run.run_id}/{run.attempts[0].attempt_id}/{uuid4().hex}",
            artifact_metadata={"mime_type": "application/json"},
        )
    ]
    client = _console_client(ConsoleRunReader([run]), FakeArtifactStore(artifact_path))

    response = client.get(
        f"/api/v1/runs/{run.run_id}/artifacts/{artifact_id}/content?download=true"
    )

    assert response.status_code == 200
    assert "workflow-report.json" in response.headers["content-disposition"]


def test_artifact_from_another_run_is_hidden(tmp_path: Path) -> None:
    owner = _run()
    other = _run()
    artifact_id = uuid4()
    owner.artifacts = [
        SimpleNamespace(
            artifact_id=artifact_id,
            attempt_id=owner.attempts[0].attempt_id,
            artifact_type="stdout",
            uri=f"runs/{owner.run_id}/{owner.attempts[0].attempt_id}/{uuid4().hex}",
            artifact_metadata={"mime_type": "text/plain"},
        )
    ]
    store = FakeArtifactStore(tmp_path / "unused")

    response = _console_client(ConsoleRunReader([owner, other]), store).get(
        f"/api/v1/runs/{other.run_id}/artifacts/{artifact_id}/content"
    )

    assert response.status_code == 404
    assert store.resolved_uris == []
    assert "runtime" not in response.text


def test_ui_shell_and_assets_are_served() -> None:
    client = _client()

    root = client.get("/", follow_redirects=False)
    page = client.get("/ui/")
    stylesheet = client.get("/ui/assets/styles.css")
    script = client.get("/ui/assets/app.js")

    assert root.status_code == 307
    assert root.headers["location"] == "/ui/"
    assert page.status_code == stylesheet.status_code == script.status_code == 200
    assert "QualityFlow" in page.text
    assert "createRun" in script.text


def test_ui_exposes_test_type_and_editable_request_body_workflow() -> None:
    client = _client()

    page = client.get("/ui/")
    script = client.get("/ui/assets/app.js")

    assert page.status_code == script.status_code == 200
    assert 'id="test-type-select"' in page.text
    assert 'id="request-body-section"' in page.text
    assert 'id="request-body-editor"' in page.text
    assert 'id="load-request-example"' in page.text
    assert 'id="format-request-body"' in page.text
    assert 'id="validate-request-body"' in page.text
    assert 'id="request-body-content"' in page.text
    assert '/ui/assets/app.js?v=light-console-v1' in page.text
    assert "renderSuiteOptions" in script.text
    assert "validateRequestBody" in script.text
    assert "request_body: requestBody" in script.text
    assert "run.request_body" in script.text


def test_ui_exposes_formal_operations_console_landmarks() -> None:
    client = _client()

    page = client.get("/ui/")
    script = client.get("/ui/assets/app.js")

    assert page.status_code == script.status_code == 200
    assert '/ui/assets/styles.css?v=light-console-v1' in page.text
    assert 'class="icon-sprite"' in page.text
    assert 'id="environment-label"' in page.text
    assert 'id="overview-total"' in page.text
    assert 'id="overview-running"' in page.text
    assert 'id="overview-passed"' in page.text
    assert 'id="overview-attention"' in page.text
    assert 'id="execution-summary"' in page.text
    assert 'id="summary-test-type"' in page.text
    assert 'id="summary-suite"' in page.text
    assert "renderOverview" in script.text
    assert "updateExecutionSummary" in script.text


def test_ui_exposes_light_console_navigation_and_real_data_views() -> None:
    client = _client()

    page = client.get("/ui/")
    script = client.get("/ui/assets/app.js")

    assert page.status_code == script.status_code == 200
    assert '<meta name="color-scheme" content="light">' in page.text
    assert '/ui/assets/styles.css?v=light-console-v1' in page.text
    assert '/ui/assets/app.js?v=light-console-v1' in page.text
    assert 'id="view-overview"' in page.text
    assert 'id="overview-recent-body"' in page.text
    assert 'id="view-suites"' in page.text
    assert 'id="suite-catalog"' in page.text
    assert "运行概览" in page.text
    assert "运行任务" in page.text
    assert "套件目录" in page.text
    assert 'href="/docs"' in page.text
    assert "renderOverviewRecent" in script.text
    assert "renderSuiteCatalog" in script.text


def test_ui_assets_are_included_in_the_installed_python_package() -> None:
    project_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["tool"]["setuptools"]["package-data"]["quality_flow"] == [
        "api/static/*"
    ]
