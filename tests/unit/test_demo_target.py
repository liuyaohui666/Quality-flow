from __future__ import annotations

from fastapi.testclient import TestClient

from demo_target.app import create_app


def test_health_and_immediate_work_modes_are_deterministic() -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = TestClient(create_app(sleep=record_sleep))

    assert client.get("/health").json() == {"status": "ok"}
    for mode in ("ok", "baseline"):
        response = client.get("/work", params={"mode": mode})
        assert response.status_code == 200
        assert response.json() == {"mode": mode, "status": "ok"}
    assert sleeps == []


def test_error_mode_returns_a_json_500_without_raising() -> None:
    client = TestClient(create_app())

    response = client.get("/work", params={"mode": "error"})

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"mode": "error", "status": "error"}


def test_slow_and_degraded_modes_use_fixed_async_delays() -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = TestClient(create_app(sleep=record_sleep))

    for mode in ("slow", "degraded"):
        response = client.get("/work", params={"mode": mode})
        assert response.status_code == 200
        assert response.json() == {"mode": mode, "status": "ok"}
    assert sleeps == [5.0, 0.35]


def test_missing_and_unknown_modes_are_rejected_as_4xx() -> None:
    client = TestClient(create_app())

    assert client.get("/work").status_code == 422
    assert client.get("/work", params={"mode": "random"}).status_code == 422


def test_workflow_resource_supports_a_deterministic_crud_lifecycle() -> None:
    client = TestClient(create_app())

    created = client.post("/workflow/resources", json={"name": "Ada"})

    assert created.status_code == 201
    assert created.json() == {"id": "resource-1", "name": "Ada"}
    resource_id = created.json()["id"]
    assert client.get(f"/workflow/resources/{resource_id}").json() == created.json()

    updated = client.patch(
        f"/workflow/resources/{resource_id}", json={"name": "Grace"}
    )
    assert updated.status_code == 200
    assert updated.json() == {"id": "resource-1", "name": "Grace"}

    removed = client.delete(f"/workflow/resources/{resource_id}")
    assert removed.status_code == 204
    assert client.get(f"/workflow/resources/{resource_id}").status_code == 404


def test_workflow_resources_are_isolated_per_demo_app() -> None:
    first = TestClient(create_app())
    second = TestClient(create_app())

    first.post("/workflow/resources", json={"name": "Only in first"})

    assert first.get("/workflow/resources/resource-1").status_code == 200
    assert second.get("/workflow/resources/resource-1").status_code == 404
