from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SERVICES = {
    "postgres",
    "redis",
    "migrate",
    "api",
    "dispatcher",
    "worker",
    "reconciler",
    "demo-target",
}


def _compose() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_compose_has_exact_isolated_service_and_volume_topology() -> None:
    compose = _compose()

    assert set(compose["services"]) == EXPECTED_SERVICES
    assert set(compose["volumes"]) == {
        "quality-flow-postgres",
        "quality-flow-redis",
        "quality-flow-artifacts",
    }
    assert compose["services"]["postgres"].get("ports") is None
    assert compose["services"]["redis"].get("ports") is None
    assert compose["services"]["demo-target"].get("ports") is None
    assert compose["services"]["api"]["ports"] == [
        "127.0.0.1:${QUALITY_FLOW_API_PORT:-18000}:8000"
    ]


def test_application_roles_share_one_image_and_have_explicit_safe_commands() -> None:
    compose = _compose()
    services = compose["services"]
    application_roles = ("migrate", "api", "dispatcher", "worker", "reconciler")

    assert {services[name]["image"] for name in application_roles} == {
        "quality-flow:local"
    }
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["api"]["command"][:3] == ["uvicorn", "quality_flow.api.app:app", "--host"]
    assert services["dispatcher"]["command"] == [
        "python",
        "-m",
        "quality_flow.runtime",
        "dispatcher",
    ]
    assert services["reconciler"]["command"] == [
        "python",
        "-m",
        "quality_flow.runtime",
        "reconciler",
    ]
    assert "quality_flow.worker.tasks:celery_app" in services["worker"]["command"]
    assert "--concurrency=1" in services["worker"]["command"]
    assert "--queues=quality-flow" in services["worker"]["command"]


def test_compose_builds_the_shared_application_image_once() -> None:
    services = _compose()["services"]

    assert [name for name, service in services.items() if "build" in service] == [
        "api"
    ]


def test_compose_uses_internal_dns_health_gates_and_no_docker_escape_hatches() -> None:
    compose = _compose()
    services = compose["services"]
    rendered = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    for name, service in services.items():
        assert service.get("privileged") is not True
        assert service.get("network_mode") != "host"
        assert all("/var/run/docker.sock" not in str(volume) for volume in service.get("volumes", []))
    assert "localhost" not in rendered
    assert "postgres:5432" in rendered
    assert "redis:6379" in rendered
    assert "http://demo-target:8000" in rendered
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    for role in ("api", "dispatcher", "worker", "reconciler"):
        assert services[role]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["worker"]["depends_on"]["demo-target"]["condition"] == "service_healthy"


def test_image_is_python_312_non_root_and_build_context_excludes_host_state() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    ignored = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert dockerfile.startswith("FROM python:3.12-slim")
    assert "WORKDIR /app" in dockerfile
    assert "USER quality-flow" in dockerfile
    assert 'pip install --no-cache-dir ".[dev]"' in dockerfile
    for pattern in (".git", ".venv", "__pycache__", ".env", "reports", "runtime"):
        assert pattern in ignored
