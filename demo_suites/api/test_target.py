"""Deterministic functional checks for the local QualityFlow demo target."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen


def test_registered_scenario_matches_expected_target_behavior() -> None:
    target_url = os.environ["QUALITY_FLOW_TARGET_URL"].rstrip("/")
    scenario = os.environ["QUALITY_FLOW_PARAM_SCENARIO"]
    request_url = f"{target_url}/work?{urlencode({'mode': scenario})}"

    try:
        with urlopen(request_url, timeout=30) as response:  # noqa: S310 - fixed local target
            status_code = response.status
            payload = json.load(response)
    except HTTPError as error:
        status_code = error.code
        payload = json.load(error)

    assert status_code == 200
    assert payload == {"mode": scenario, "status": "ok"}
