from __future__ import annotations

from pathlib import Path
import re
import tomllib

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "quality-flow.yml"
ALLOWED_ACTIONS = {
    "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
    "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _run_commands(job: dict) -> str:
    return "\n".join(
        str(step["run"]) for step in job["steps"] if "run" in step
    )


def test_python_and_ruff_policy_match_the_delivery_runtime() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert project["project"]["requires-python"] == ">=3.12"
    assert "ruff==0.16.2" in project["project"]["optional-dependencies"]["dev"]
    assert project["tool"]["ruff"]["target-version"] == "py312"
    assert project["tool"]["ruff"]["lint"]["select"] == ["E4", "E7", "E9", "F"]


def test_restful_booker_delivery_dependency_and_e2e_boundary() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    e2e_commands = _run_commands(_workflow()["jobs"]["e2e"])

    assert "requests>=2.31,<3" in project["project"]["dependencies"]
    assert "restful-booker-api" not in e2e_commands
    assert "restful-booker.herokuapp.com" not in e2e_commands


def test_generated_ci_evidence_is_not_committable() -> None:
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".ci-evidence/" in ignored


def test_workflow_uses_read_only_sha_pinned_bounded_jobs() -> None:
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = _workflow()

    assert "pull_request_target" not in raw
    assert "secrets." not in raw
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"quality", "integration", "e2e"}
    assert workflow["concurrency"]["cancel-in-progress"] is True

    used_actions: set[str] = set()
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "ubuntu-24.04"
        assert 1 <= job["timeout-minutes"] <= 30
        for step in job["steps"]:
            if "uses" in step:
                used_actions.add(step["uses"])
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False

    assert used_actions == ALLOWED_ACTIONS
    assert all(re.search(r"@[0-9a-f]{40}$", action) for action in used_actions)


def test_workflow_propagates_failures_through_tee_pipelines() -> None:
    workflow = _workflow()

    assert workflow["defaults"]["run"]["shell"] == "bash"


def test_workflow_runs_static_integration_and_full_e2e_gates() -> None:
    jobs = _workflow()["jobs"]
    quality = _run_commands(jobs["quality"])
    integration = _run_commands(jobs["integration"])
    e2e = _run_commands(jobs["e2e"])

    assert 'python -m pip install ".[dev]"' in quality
    assert "python -m pip check" in quality
    assert "python -m ruff check ." in quality
    assert "python -m pytest tests/unit" in quality
    assert "test_posix_timeout_reaps_a_spawned_child_process" in quality

    assert "docker compose -p \"$COMPOSE_PROJECT\" build --pull --no-cache api" in integration
    assert "docker compose -p \"$COMPOSE_PROJECT\" up -d --wait" in integration
    assert "python -m pytest tests/integration" in integration
    assert "QUALITY_FLOW_TEST_ADMIN_DATABASE_URL" in integration
    assert "/app/tests:ro" in integration

    assert "docker compose -p \"$COMPOSE_PROJECT\" up -d --wait" in e2e
    assert "python -m pytest tests/e2e" in e2e
    assert "scripts/ci_gate.py" in e2e
    assert "expected_failure_rc" in e2e
    assert 'test "$expected_failure_rc" -eq 1' in e2e
    assert "/health/live" in e2e
    assert "/health/ready" in e2e


def test_workflow_audits_evidence_before_upload_and_scoped_cleanup() -> None:
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = _workflow()

    assert "docker system prune" not in raw
    assert "docker volume prune" not in raw
    assert "printenv" not in raw
    for line in raw.splitlines():
        if "docker compose" in line and "down -v" in line:
            assert '-p "$COMPOSE_PROJECT"' in line

    for job_name, job in workflow["jobs"].items():
        names = [step["name"] for step in job["steps"]]
        audit_index = names.index("Audit retained evidence")
        upload_index = names.index("Upload retained evidence")
        assert audit_index < upload_index
        audit = job["steps"][audit_index]
        upload = job["steps"][upload_index]
        assert audit["if"] == "always()"
        assert "steps.audit.outcome == 'success'" in upload["if"]
        assert upload["with"]["retention-days"] == 14
        assert upload["with"]["if-no-files-found"] == "warn"
        if job_name in {"integration", "e2e"}:
            diagnostics_index = names.index("Collect scoped diagnostics")
            cleanup_index = names.index("Remove scoped Compose resources")
            assert diagnostics_index < audit_index < upload_index < cleanup_index
            diagnostics = job["steps"][diagnostics_index]
            assert diagnostics["if"] == "always()"
            assert (
                f"| tee .ci-evidence/{job_name}/compose.log" in diagnostics["run"]
            )
            assert job["steps"][cleanup_index]["if"] == "always()"


def test_delivery_documents_keep_claims_and_commands_truthful() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (PROJECT_ROOT / "docs" / "architecture.md").read_text(
        encoding="utf-8"
    )
    matrix = (PROJECT_ROOT / "docs" / "evidence-matrix.md").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((readme, architecture, matrix))

    for pair in (
        "completed/passed",
        "completed/failed",
        "timed_out/unknown",
        "infra_failed/unknown",
    ):
        assert pair in combined
    for scenario in ("demo-api / ok", "demo-api / error", "demo-api / slow", "demo-load / baseline", "demo-load / degraded"):
        assert scenario in readme

    assert "at-least-once" in combined
    assert "exactly-once" in combined.lower()
    assert "可信套件" in combined
    assert "不是恶意代码安全沙箱" in combined
    assert "不提供 Artifact 文件下载接口" in combined
    assert "GitHub 托管 Ubuntu Runner 已完成" in readme
    assert "认证/RBAC" in readme
    assert "自动重试/取消" in readme
    assert "高可用/灾备" in readme
    assert "多节点压测" in readme
    assert "生产部署" in readme

    expected_header = (
        "| Claim | Implementation | Verification command | Evidence artifact | Limitation |"
    )
    assert expected_header in matrix
    assert matrix.count("\n|") >= 10
    for claim in (
        "逻辑幂等",
        "Outbox 恢复",
        "超时分类",
        "租约过期",
        "pytest 功能门禁",
        "Locust 性能门禁",
        "Artifact 隔离",
        "CI 退出码",
    ):
        assert claim in matrix

    for document in (readme, architecture, matrix):
        for line in document.splitlines():
            if "docker compose" in line and "down -v" in line:
                assert "-p" in line


def test_design_spec_matches_the_implemented_v1_boundary() -> None:
    design = (
        PROJECT_ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-08-09-quality-flow-design.md"
    ).read_text(encoding="utf-8")

    assert "V1 固定使用并验证 Python 3.12" in design
    assert "公开 API 只返回安全元数据" in design
    assert "不提供 Artifact 文件下载接口" in design
    assert "单 Artifact 文件上限" in design
    assert "单 Run 总量限制与垃圾回收尚未实现" in design
    assert "Compose/进程文本日志" in design
    assert "统一 JSON 结构化日志" not in design
    assert "真实秘密不得进入 Git" in design
    assert "公开的本地演示默认值" in design


def test_migration_roundtrip_uses_a_configurable_admin_database_url() -> None:
    integration_test = (
        PROJECT_ROOT / "tests" / "integration" / "test_worker_lifecycle.py"
    ).read_text(encoding="utf-8")
    function = integration_test.split(
        "def test_attempt_lease_migration_is_reversible_in_isolated_database", 1
    )[1]

    assert "QUALITY_FLOW_TEST_ADMIN_DATABASE_URL" in function
    assert "127.0.0.1:55432" not in function
