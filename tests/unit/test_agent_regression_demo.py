"""Exercise the real demo, evaluator and registered YAML without external services."""

import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import httpx
import pytest

from demo_target.app import create_app
from quality_flow.domain.enums import AttemptStatus
from quality_flow.runners.agent_eval_runner import AgentEvalRunner
from quality_flow.runners.base import ExecutionSpec
from quality_flow.suites.registry import SuiteRegistry


@pytest.mark.parametrize("scenario,failed_case,reason", [
    ("normal", None, None),
    ("forgot_context", "context-memory", "answer missing fragments"),
    ("bad_tool_arguments", "weather-then-calendar", "arguments violate schema"),
    ("injection_bypass", "injection-refusal", "disallowed tools"),
])
def test_registered_agent_regression_detects_the_selected_fault(
    tmp_path: Path, scenario: str, failed_case: str | None, reason: str | None,
) -> None:
    root = Path(__file__).resolve().parents[2]
    suite = SuiteRegistry.from_yaml(root / "config/suites.yaml", root).get("demo-agent-regression")
    shutil.copyfile(suite.working_directory / "regression.yaml", tmp_path / "regression.yaml")
    payload = suite.resolve_request_body({"topic": "quality engineering", "scenario": scenario})
    with TestClient(create_app()) as target:
        def forward(request: httpx.Request) -> httpx.Response:
            return target.post(str(request.url), json=json.loads(request.content))

        result = AgentEvalRunner(
            environment={"QUALITY_FLOW_TARGET_URL": "http://testserver"},
            transport=httpx.MockTransport(forward),
            staging_root=tmp_path.parent / f"{tmp_path.name}-staging",
        ).run(ExecutionSpec(
            argv=suite.argv, timeout_seconds=30, allowed_workspace_root=tmp_path,
            request_body=payload, gate_policy=suite.gate_policy,
        ), tmp_path, lambda: None)

    expected_status = AttemptStatus.TEST_FAILED if failed_case else AttemptStatus.PASSED
    assert result.attempt_status is expected_status
    assert result.gate_result.passed is (failed_case is None)
    report = json.loads(result.artifacts[0].source_path.read_text(encoding="utf-8"))
    assert report["query_parameters"] == {"scenario": scenario}
    assert [case["id"] for case in report["cases"] if case["status"] != "passed"] == (
        [failed_case] if failed_case else []
    )
    if failed_case:
        case = next(case for case in report["cases"] if case["id"] == failed_case)
        assert reason in case["samples"][0]["message"]
        if scenario == "forgot_context":
            assert case["safety_violation_samples"] == []
        else:
            assert case["safety_violation_samples"] == [1, 2, 3]
