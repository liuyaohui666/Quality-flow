"""Configuration loading helpers."""

import os
import re
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping and reject empty or non-mapping documents."""
    with path.open(encoding="utf-8") as config_file:
        content = yaml.safe_load(config_file)
    if not isinstance(content, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return content


_ENVIRONMENT_VALUE = re.compile(r"^\$\{(?P<name>[A-Z0-9_]+):-?(?P<default>[^}]*)\}$")


def resolve_environment_variables(value: Any) -> Any:
    """Recursively resolve ${NAME:-default} strings."""
    if isinstance(value, dict):
        return {key: resolve_environment_variables(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_environment_variables(item) for item in value]
    if isinstance(value, str):
        match = _ENVIRONMENT_VALUE.match(value)
        if match:
            return os.getenv(match.group("name")) or match.group("default")
    return value
