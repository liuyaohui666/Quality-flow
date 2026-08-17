"""Core public-environment CRUD regression coverage."""

from copy import deepcopy

import pytest

from clients.booking_client import BookingClient
from utils.assertions import assert_json_contains, assert_status_code


@pytest.mark.api
@pytest.mark.smoke
def test_create_booking_then_get_details(
    created_booking: tuple[int, dict], booking_client: BookingClient
) -> None:
    booking_id, expected_booking = created_booking

    response = booking_client.get_booking(booking_id)

    assert_status_code(response, 200)
    assert_json_contains(response, expected_booking)


@pytest.mark.api
def test_list_bookings_can_filter_by_firstname(
    created_booking: tuple[int, dict], booking_client: BookingClient
) -> None:
    booking_id, booking = created_booking

    response = booking_client.list_bookings({"firstname": booking["firstname"]})

    assert_status_code(response, 200)
    assert {"bookingid": booking_id} in response.json()


@pytest.mark.api
def test_full_update_replaces_booking(
    created_booking: tuple[int, dict], booking_client: BookingClient, booking_data: dict, auth_token: str
) -> None:
    booking_id, _ = created_booking
    updated_booking = deepcopy(booking_data["full_update_booking"])

    response = booking_client.update_booking(booking_id, updated_booking, token=auth_token)

    assert_status_code(response, 200)
    assert_json_contains(response, updated_booking)


@pytest.mark.api
def test_partial_update_changes_only_requested_field(
    created_booking: tuple[int, dict], booking_client: BookingClient, auth_token: str
) -> None:
    booking_id, original_booking = created_booking

    response = booking_client.partial_update_booking(booking_id, {"firstname": "Katherine"}, auth_token)

    assert_status_code(response, 200)
    assert_json_contains(response, {"firstname": "Katherine", "lastname": original_booking["lastname"]})


@pytest.mark.api
def test_delete_booking_makes_resource_unavailable(
    created_booking: tuple[int, dict], booking_client: BookingClient, auth_token: str
) -> None:
    booking_id, _ = created_booking

    delete_response = booking_client.delete_booking(booking_id, auth_token)
    get_response = booking_client.get_booking(booking_id)

    assert_status_code(delete_response, 201)
    assert_status_code(get_response, 404)


@pytest.mark.api
@pytest.mark.parametrize("booking_id", [-1, 999999999])
def test_get_missing_booking_returns_not_found(booking_client: BookingClient, booking_id: int) -> None:
    response = booking_client.get_booking(booking_id)

    assert_status_code(response, 404)
