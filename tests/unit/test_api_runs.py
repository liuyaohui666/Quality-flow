from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from quality_flow.api import dependencies as dependency_module
from quality_flow.api.app import create_app
from quality_flow.api.dependencies import ApiDependencies, build_dependencies
from quality_flow.application.run_service import NewRun
from quality_flow.domain.enums import RunOutcome, RunStatus
from quality_flow.suites.registry import InvalidSuiteParameter, UnknownSuiteError
from quality_flow.suites.registry import SuiteRegistry


@dataclass
class FakeRun:
    run_id: UUID = field(default_factory=uuid4)
    suite_id: str = "demo-api"
    status: RunStatus = RunStatus.QUEUED
    outcome: RunOutcome = RunOutcome.UNKNOWN
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempts: list[Any] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)
    case_results: list[Any] = field(default_factory=list)
    metrics: list[Any] = field(default_factory=list)
    gates: list[Any] = field(default_factory=list)
    artifacts: list[Any] = field(default_factory=list)


class FakeRunService:
    def __init__(self) -> None:
        self.runs_by_key: dict[str, FakeRun] = {}

    def create_run(
        self, suite_id: str, idempotency_key: str, parameters: dict[str, str]
    ) -> FakeRun:
        if suite_id == "shell":
            raise UnknownSuiteError("Unknown suite: shell")
        if parameters.get("scenario") == "not-allowed":
            raise InvalidSuiteParameter("not allowlisted")
        return self.runs_by_key.setdefault(idempotency_key, FakeRun(suite_id=suite_id))


class FakeRunReader:
    def __init__(self, run_service: FakeRunService) -> None:
        self.run_service = run_service

    def get_run(self, run_id: UUID) -> FakeRun | None:
        return next(
            (run for run in self.run_service.runs_by_key.values() if run.run_id == run_id),
            None,
        )


@pytest.fixture
def client() -> TestClient:
    service = FakeRunService()
    dependencies = ApiDependencies(
        run_service=service,
        run_reader=FakeRunReader(service),
        readiness_check=lambda: None,
    )
    return TestClient(create_app(dependencies))


def test_submit_registered_suite_returns_202_and_duplicate_key_returns_same_run(
    client: TestClient,
) -> None:
    request = {
        "suite_id": "demo-api",
        "parameters": {"scenario": "smoke"},
    }
    headers = {"Idempotency-Key": "ci-123"}

    first = client.post("/api/v1/runs", headers=headers, json=request)
    second = client.post("/api/v1/runs", headers=headers, json=request)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["status"] == "queued"


@pytest.mark.parametrize(
    ("headers", "payload", "status_code"),
    [
        ({}, {"suite_id": "demo-api", "parameters": {}}, 422),
        ({"Idempotency-Key": "   "}, {"suite_id": "demo-api", "parameters": {}}, 422),
        ({"Idempotency-Key": "x" * 256}, {"suite_id": "demo-api", "parameters": {}}, 422),
        ({"Idempotency-Key": "known"}, {"suite_id": "shell", "parameters": {}}, 404),
        (
            {"Idempotency-Key": "raw-command"},
            {"suite_id": "demo-api", "parameters": {}, "command": "rm -rf /"},
            422,
        ),
        (
            {"Idempotency-Key": "bad-parameter"},
            {"suite_id": "demo-api", "parameters": {"scenario": "not-allowed"}},
            422,
        ),
    ],
)
def test_submit_maps_validation_and_domain_errors_without_500(
    client: TestClient, headers: dict[str, str], payload: dict[str, Any], status_code: int
) -> None:
    response = client.post("/api/v1/runs", headers=headers, json=payload)

    assert response.status_code == status_code


def test_run_query_contract_hides_local_paths_and_unknown_run_is_404(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": "read-contract"},
        json={"suite_id": "demo-api", "parameters": {}},
    ).json()

    response = client.get(f"/api/v1/runs/{created['run_id']}")
    unknown = client.get(f"/api/v1/runs/{uuid4()}")

    assert response.status_code == 200
    body = response.json()
    assert {"status", "outcome", "timestamps", "attempts", "case_summary", "metrics", "gates", "artifacts"} <= body.keys()
    assert "working_directory" not in str(body)
    assert unknown.status_code == 404


def test_artifact_contract_exposes_safe_metadata_without_uri_or_secrets() -> None:
    run = FakeRun()
    artifact = SimpleNamespace(
        artifact_id=uuid4(),
        attempt_id=uuid4(),
        artifact_type="junit",
        uri=r"D:\attempts\private\junit.xml",
        checksum="abc123",
        artifact_metadata={
            "size_bytes": 321,
            "mime_type": "application/xml",
            "token": "super-secret",
        },
        created_at=datetime.now(UTC),
    )
    run.artifacts = [artifact]

    class ArtifactReader:
        def get_run(self, run_id: UUID) -> FakeRun | None:
            return run if run_id == run.run_id else None

    app = create_app(
        ApiDependencies(
            run_service=FakeRunService(),
            run_reader=ArtifactReader(),
            readiness_check=lambda: None,
        )
    )

    response = TestClient(app).get(f"/api/v1/runs/{run.run_id}/artifacts")

    assert response.status_code == 200
    assert response.json()["artifacts"] == [
        {
            "artifact_id": str(artifact.artifact_id),
            "attempt_id": str(artifact.attempt_id),
            "artifact_type": "junit",
            "checksum": "abc123",
            "size_bytes": 321,
            "mime_type": "application/xml",
            "created_at": artifact.created_at.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert "private" not in response.text
    assert "super-secret" not in response.text


def test_events_artifacts_and_health_contracts(client: TestClient) -> None:
    created = client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": "related-contract"},
        json={"suite_id": "demo-api", "parameters": {}},
    ).json()

    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get(f"/api/v1/runs/{created['run_id']}/events").json() == {"events": []}
    assert client.get(f"/api/v1/runs/{created['run_id']}/artifacts").json() == {"artifacts": []}


def test_submit_reads_persisted_run_before_serializing_new_run() -> None:
    persisted = FakeRun()
    created = NewRun(
        run_id=persisted.run_id,
        suite_id=persisted.suite_id,
        idempotency_key="new-run-key",
        parameters={},
        suite_snapshot={},
        gate_policy_snapshot={},
        status=RunStatus.QUEUED,
        outcome=RunOutcome.UNKNOWN,
        version=1,
        created_at=persisted.created_at,
    )

    class NewRunService:
        def create_run(self, *_args: object, **_kwargs: object) -> NewRun:
            return created

    class PersistedRunReader:
        def __init__(self) -> None:
            self.requested_ids: list[UUID] = []

        def get_run(self, run_id: UUID) -> FakeRun | None:
            self.requested_ids.append(run_id)
            return persisted if run_id == persisted.run_id else None

    reader = PersistedRunReader()
    app = create_app(
        ApiDependencies(
            run_service=NewRunService(),  # type: ignore[arg-type]
            run_reader=reader,
            readiness_check=lambda: None,
        )
    )

    response = TestClient(app).post(
        "/api/v1/runs",
        headers={"Idempotency-Key": "new-run-key"},
        json={"suite_id": "demo-api", "parameters": {}},
    )

    assert response.status_code == 202
    assert response.json()["run_id"] == str(created.run_id)
    assert reader.requested_ids == [created.run_id]


def test_dependency_builder_uses_existing_database_url_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_url = (
        "postgresql+psycopg://quality_flow:quality_flow@localhost:55432/quality_flow"
    )
    captured_urls: list[str] = []
    fake_engine = object()
    monkeypatch.setenv("DATABASE_URL", expected_url)
    monkeypatch.delenv("QUALITY_FLOW_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        dependency_module,
        "make_engine",
        lambda url: captured_urls.append(url) or fake_engine,
    )
    monkeypatch.setattr(
        dependency_module,
        "make_session_factory",
        lambda engine: object(),
    )

    build_dependencies()

    assert captured_urls == [expected_url]


def test_ready_is_unavailable_when_suite_registry_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(dependency_module, "make_engine", lambda _url: object())
    monkeypatch.setattr(
        dependency_module, "make_session_factory", lambda _engine: FakeSession
    )
    monkeypatch.setattr(
        dependency_module.SuiteRegistry,
        "from_yaml",
        lambda *_args: SuiteRegistry({}),
    )
    dependencies = build_dependencies()

    response = TestClient(create_app(dependencies)).get("/health/ready")

    assert response.status_code == 503


def test_ready_checks_redis_as_well_as_postgres_and_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeSession:
        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object) -> None:
            calls.append("postgres")

    class FakeRedis:
        def ping(self) -> bool:
            calls.append("redis")
            return True

    monkeypatch.setattr(dependency_module, "make_engine", lambda _url: object())
    monkeypatch.setattr(
        dependency_module, "make_session_factory", lambda _engine: FakeSession
    )
    monkeypatch.setattr(
        dependency_module.SuiteRegistry,
        "from_yaml",
        lambda *_args: SuiteRegistry({"demo-api": object()}),
    )
    monkeypatch.setattr(
        dependency_module.Redis,
        "from_url",
        lambda _url, **_kwargs: FakeRedis(),
    )

    dependencies = build_dependencies()
    response = TestClient(create_app(dependencies)).get("/health/ready")

    assert response.status_code == 200
    assert calls == ["postgres", "redis"]


def test_dependency_builder_bounds_redis_readiness_socket_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(dependency_module, "make_engine", lambda _url: object())
    monkeypatch.setattr(
        dependency_module, "make_session_factory", lambda _engine: object()
    )
    monkeypatch.setattr(
        dependency_module.Redis,
        "from_url",
        lambda url, **kwargs: captured.update(url=url, **kwargs) or object(),
    )

    build_dependencies()

    assert captured["socket_connect_timeout"] == 2.0
    assert captured["socket_timeout"] == 2.0
