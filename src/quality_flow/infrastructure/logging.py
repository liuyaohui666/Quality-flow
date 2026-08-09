"""Structured-log helpers that redact secrets before serialization."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from enum import Enum
import math
from pathlib import Path
import re
from typing import Any
from uuid import UUID


_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "x_api_key",
        "secret",
        "client_secret",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
    }
)
_NORMALIZED_SENSITIVE_KEYS = frozenset(
    key.replace("_", "") for key in _SENSITIVE_KEYS
)


def redact(value: Any) -> Any:
    """Return a recursively redacted copy without modifying the input value."""
    if isinstance(value, Mapping):
        return {
            key: "***" if _is_sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def redact_for_log(value: Any) -> Any:
    """Produce a JSON-safe, redacted structure for structured logging."""
    return redact(_to_log_value(value))


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    return re.sub(r"[-_]", "", key.casefold()) in _NORMALIZED_SENSITIVE_KEYS


def _to_log_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _to_log_value(value.value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Mapping):
        return {str(key): _to_log_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_to_log_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return sorted((_to_log_value(item) for item in value), key=repr)
    return {"type": type(value).__name__}
