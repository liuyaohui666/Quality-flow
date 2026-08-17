from __future__ import annotations

from pathlib import Path
import shutil

from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.base import ExecutionSpec
from quality_flow.runners.pytest_runner import PytestRunner
from quality_flow.suites.registry import SuiteRegistry


def test_pytest_runner_executes_restful_booker_offline_unit_suite(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    source_suite = project_root / "demo_suites" / "restful_booker"
    workspace = tmp_path / "workspace"
    shutil.copytree(
        source_suite,
        workspace,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "logs"),
    )
    registry = SuiteRegistry.from_yaml(
        project_root / "config" / "suites.yaml", project_root
    )
    suite = registry.get("restful-booker-api")
    spec = ExecutionSpec(
        argv=(
            "python",
            "-m",
            "pytest",
            "tests/test_booking_client.py",
            "tests/test_assertions.py",
            "tests/test_config.py",
            "tests/test_logger.py",
            "-q",
            "--strict-markers",
        ),
        timeout_seconds=suite.timeout_seconds,
        allowed_workspace_root=tmp_path,
        parameters=suite.resolve_parameters({}),
        gate_policy=suite.gate_policy,
    )

    outcome = PytestRunner(staging_root=tmp_path / "staging").run(
        spec, workspace, lambda: None
    )

    assert outcome.attempt_status is AttemptStatus.PASSED
    assert outcome.case_summary is not None
    assert outcome.case_summary.total >= 8
    assert outcome.case_summary.failed == 0
    assert outcome.case_summary.errors == 0
    assert outcome.gate_result is not None and outcome.gate_result.passed
    assert {item.artifact_type for item in outcome.artifacts} == {
        "stdout",
        "stderr",
        "junit_xml",
    }
