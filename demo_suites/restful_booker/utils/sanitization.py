"""Shared sanitization for HTTP evidence emitted by the suite."""

from collections.abc import Mapping
import json
from typing import Any


_SENSITIVE_KEYS = frozenset(
    {"password", "token", "authorization", "cookie", "set-cookie"}
)
_UNLOGGABLE_RESPONSE_BODY = "<response body omitted: not valid JSON>"


def redact_payload(payload: Any) -> Any:
    """Copy nested data while replacing values associated with sensitive keys."""
    if isinstance(payload, Mapping):
        return {
            key: (
                "***"
                if isinstance(key, str) and key.casefold() in _SENSITIVE_KEYS
                else redact_payload(value)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact_payload(item) for item in payload)
    return payload


def sanitized_json(payload: Any) -> str:
    """Serialize nested data after recursively removing sensitive values."""
    return json.dumps(redact_payload(payload), ensure_ascii=False, default=str)


def sanitized_response_body(response: Any) -> str:
    """Return safe response evidence, omitting bodies that cannot be parsed as JSON."""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return _UNLOGGABLE_RESPONSE_BODY
    return sanitized_json(payload)
