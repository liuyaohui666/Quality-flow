from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_launcher_is_project_scoped_and_non_destructive() -> None:
    script = (PROJECT_ROOT / "qualityflow.ps1").read_text(encoding="utf-8")

    assert '"quality-flow-demo"' in script
    assert '"start", "stop", "status", "logs"' in script
    assert "--remove-orphans" in script
    assert "http://127.0.0.1:18000/ui/" in script
    assert "--wait-timeout" in script
    assert "prune" not in script.lower()
    assert not re.search(r"\bdown\b[^\n]*\s-v(?:\s|$)", script)


def test_local_brainstorm_state_is_ignored_by_git_and_docker() -> None:
    for filename in (".gitignore", ".dockerignore"):
        patterns = (PROJECT_ROOT / filename).read_text(encoding="utf-8").splitlines()
        assert ".superpowers/" in patterns

