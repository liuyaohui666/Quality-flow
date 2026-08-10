"""Single-process load scenario for the local QualityFlow demo target."""

from __future__ import annotations

import os

from locust import HttpUser, between, task


class DemoTargetUser(HttpUser):
    host = os.environ["QUALITY_FLOW_TARGET_URL"].rstrip("/")
    wait_time = between(0.01, 0.02)

    @task
    def exercise_registered_scenario(self) -> None:
        scenario = os.environ["QUALITY_FLOW_PARAM_SCENARIO"]
        with self.client.get(
            "/work",
            params={"mode": scenario},
            name=f"/work?mode={scenario}",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"unexpected HTTP status {response.status_code}")
