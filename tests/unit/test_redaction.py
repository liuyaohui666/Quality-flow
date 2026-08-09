from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from quality_flow.infrastructure.logging import redact, redact_for_log


def test_redact_masks_sensitive_keys_recursively_without_mutating_input() -> None:
    original = {
        "headers": {"Authorization": "not-for-logs", "Cookie": "session=not-for-logs"},
        "items": ({"API_KEY": "not-for-logs"}, ["keep", {"token": "not-for-logs"}]),
    }

    redacted = redact(original)

    assert redacted == {
        "headers": {"Authorization": "***", "Cookie": "***"},
        "items": ({"API_KEY": "***"}, ["keep", {"token": "***"}]),
    }
    assert original["headers"]["Authorization"] == "not-for-logs"
    assert isinstance(redacted["items"], tuple)


def test_redactor_matches_common_sensitive_keys_case_insensitively() -> None:
    value = {
        "Password": "not-for-logs",
        "passwd": "not-for-logs",
        "access_token": "not-for-logs",
        "secret": "not-for-logs",
        "Set-Cookie": "not-for-logs",
        "ordinary": "keep",
    }
    assert redact(value) == {
        "Password": "***",
        "passwd": "***",
        "access_token": "***",
        "secret": "***",
        "Set-Cookie": "***",
        "ordinary": "keep",
    }


def test_log_helper_redacts_camel_case_keys_and_serializes_unknown_values() -> None:
    payload = redact_for_log(
        {
            "accessToken": "not-for-logs",
            "apiKey": "not-for-logs",
            "clientSecret": "not-for-logs",
            "id": uuid4(),
            "labels": {"one", "two"},
        }
    )

    assert payload["accessToken"] == "***"
    assert payload["apiKey"] == "***"
    assert payload["clientSecret"] == "***"
    json.dumps(payload, allow_nan=False)

def test_log_helper_converts_paths_and_exceptions_to_json_safe_values() -> None:
    payload = redact_for_log(
        {"path": Path("private-result.xml"), "error": ValueError("parse failed")}
    )

    assert payload["path"] == "private-result.xml"
    assert payload["error"] == {"type": "ValueError", "message": "parse failed"}
    json.dumps(payload)
