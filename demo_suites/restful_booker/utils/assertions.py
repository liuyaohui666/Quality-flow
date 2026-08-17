"""Readable assertions shared by API test cases."""

from typing import Any

from utils.sanitization import sanitized_response_body


def assert_status_code(response: Any, expected_status: int) -> None:
    """Assert an HTTP status and include body evidence when it differs."""
    assert response.status_code == expected_status, (
        f"Expected status {expected_status}, got {response.status_code}. "
        f"Response body: {sanitized_response_body(response)}"
    )


def assert_json_contains(response: Any, expected: dict[str, Any]) -> None:
    """Assert that all expected nested key/value pairs occur in JSON output."""
    _assert_mapping_contains(response.json(), expected)


def _assert_mapping_contains(actual: dict[str, Any], expected: dict[str, Any], path: str = "response") -> None:
    for key, expected_value in expected.items():
        current_path = f"{path}.{key}"
        assert key in actual, f"Missing key {current_path}. Actual JSON: {actual}"
        actual_value = actual[key]
        if isinstance(expected_value, dict):
            assert isinstance(actual_value, dict), f"Expected object at {current_path}, got {actual_value!r}"
            _assert_mapping_contains(actual_value, expected_value, current_path)
        else:
            assert actual_value == expected_value, (
                f"Expected {current_path}={expected_value!r}, got {actual_value!r}. Actual JSON: {actual}"
            )
