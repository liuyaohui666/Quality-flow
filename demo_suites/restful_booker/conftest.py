"""pytest fixtures for Restful Booker API tests."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from clients.booking_client import BookingClient
from utils.assertions import assert_status_code
from utils.config_loader import load_yaml, resolve_environment_variables


PROJECT_ROOT = Path(__file__).parent


@pytest.fixture(scope="session")
def settings() -> dict[str, Any]:
    return resolve_environment_variables(load_yaml(PROJECT_ROOT / "config" / "config.yaml"))


@pytest.fixture(scope="session")
def booking_data() -> dict[str, Any]:
    return load_yaml(PROJECT_ROOT / "data" / "booking_data.yaml")


@pytest.fixture(scope="session")
def booking_client(settings: dict[str, Any]) -> BookingClient:
    return BookingClient(settings["base_url"], settings["request_timeout"])


@pytest.fixture(scope="session")
def auth_token(booking_client: BookingClient, settings: dict[str, Any]) -> str:
    response = booking_client.create_token(settings["auth"])
    assert_status_code(response, 200)
    token = response.json().get("token")
    assert token, f"Token missing from authentication response: {response.text}"
    return token


@pytest.fixture
def created_booking(booking_client: BookingClient, booking_data: dict[str, Any], auth_token: str):
    """Create one booking per test and attempt cleanup after it finishes."""
    payload = deepcopy(booking_data["valid_booking"])
    response = booking_client.create_booking(payload)
    assert_status_code(response, 200)
    booking_id = response.json()["bookingid"]
    yield booking_id, payload
    booking_client.delete_booking(booking_id, auth_token)
