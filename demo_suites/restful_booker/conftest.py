"""pytest fixtures for Restful Booker API tests."""

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from clients.booking_client import BookingClient
from utils.assertions import assert_status_code
from utils.config_loader import load_yaml, resolve_environment_variables
from utils.sanitization import sanitized_response_body


PROJECT_ROOT = Path(__file__).parent


@pytest.fixture(scope="session")
def settings() -> dict[str, Any]:
    return resolve_environment_variables(load_yaml(PROJECT_ROOT / "config" / "config.yaml"))


@pytest.fixture(scope="session")
def booking_data() -> dict[str, Any]:
    data = load_yaml(PROJECT_ROOT / "data" / "booking_data.yaml")
    encoded_request_body = os.environ.get("QUALITY_FLOW_REQUEST_BODY_JSON")
    if encoded_request_body:
        request_body = json.loads(encoded_request_body)
        if not isinstance(request_body, dict):
            raise ValueError("QUALITY_FLOW_REQUEST_BODY_JSON must contain an object")
        data["valid_booking"] = request_body
    return data


@pytest.fixture(scope="session")
def booking_client(settings: dict[str, Any]) -> BookingClient:
    return BookingClient(settings["base_url"], settings["request_timeout"])


@pytest.fixture(scope="session")
def auth_token(
    booking_client: BookingClient, request: pytest.FixtureRequest
) -> str:
    settings = request.getfixturevalue("settings")
    response = booking_client.create_token(settings["auth"])
    assert_status_code(response, 200)
    token = response.json().get("token")
    assert token, (
        "Token missing from authentication response: "
        f"{sanitized_response_body(response)}"
    )
    return token


@pytest.fixture
def created_booking(
    booking_client: BookingClient,
    booking_data: dict[str, Any],
    request: pytest.FixtureRequest,
):
    """Create one booking per test and verify cleanup after it finishes."""
    auth_token = request.getfixturevalue("auth_token")
    payload = deepcopy(booking_data["valid_booking"])
    response = booking_client.create_booking(payload)
    assert_status_code(response, 200)
    booking_id = response.json()["bookingid"]
    yield booking_id, payload
    delete_response = booking_client.delete_booking(booking_id, auth_token)
    assert_status_code(delete_response, 201)
