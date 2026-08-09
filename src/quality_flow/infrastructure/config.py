"""Application configuration loaded from the environment."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Paths needed to load the validated suite registry."""

    project_root: Path
    suites_config_path: Path

    @classmethod
    def from_environment(cls, project_root: Path) -> "Settings":
        resolved_root = project_root.resolve()
        configured_path = Path(
            os.environ.get("QUALITY_FLOW_SUITES_CONFIG", "config/suites.yaml")
        )
        if configured_path.is_absolute() or ".." in configured_path.parts:
            raise ValueError("QUALITY_FLOW_SUITES_CONFIG must be a project-relative path")

        return cls(
            project_root=resolved_root,
            suites_config_path=(resolved_root / configured_path).resolve(),
        )
