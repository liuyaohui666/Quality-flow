import pytest

from utils.assertions import assert_json_contains, assert_status_code


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def test_assert_status_code_shows_response_details_on_mismatch() -> None:
    response = FakeResponse(400, {"error": "invalid"})

    with pytest.raises(AssertionError, match="Expected status 201, got 400"):
        assert_status_code(response, 201)


def test_assert_json_contains_accepts_expected_subset() -> None:
    response = FakeResponse(200, {"booking": {"firstname": "Ada", "totalprice": 100}})

    assert_json_contains(response, {"booking": {"firstname": "Ada"}})
