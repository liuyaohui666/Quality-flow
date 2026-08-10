from __future__ import annotations

from pathlib import Path

import pytest

from scripts.audit_ci_evidence import EvidenceAuditError, audit_evidence


def test_safe_allowlisted_evidence_passes(tmp_path: Path) -> None:
    (tmp_path / "junit.xml").write_text(
        '<testsuite tests="1" failures="0" />', encoding="utf-8"
    )
    (tmp_path / "compose.log").write_text(
        "api ready; run_id=2396d6c7-006b-4f39-9c10-0d6b712cce9b\n",
        encoding="utf-8",
    )
    (tmp_path / "summary.json").write_text(
        '{"status":"completed","outcome":"passed"}', encoding="utf-8"
    )

    assert audit_evidence(tmp_path, forbidden_values=("not-present",)) == 3


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("request.log", "Authorization: Bearer should-not-be-retained"),
        ("response.txt", "Set-Cookie: session=should-not-be-retained"),
        ("broker.log", "redis://user:should-not-be-retained@redis:6379/0"),
        ("canary.json", '{"value":"ci-secret-canary"}'),
    ],
)
def test_secret_bearing_text_is_rejected_without_echoing_value(
    tmp_path: Path, filename: str, content: str
) -> None:
    (tmp_path / filename).write_text(content, encoding="utf-8")

    with pytest.raises(EvidenceAuditError) as captured:
        audit_evidence(tmp_path, forbidden_values=("ci-secret-canary",))

    assert "should-not-be-retained" not in str(captured.value)
    assert "ci-secret-canary" not in str(captured.value)


@pytest.mark.parametrize("filename", (".env", "database.dump", "backup.sql"))
def test_forbidden_evidence_file_types_are_rejected(
    tmp_path: Path, filename: str
) -> None:
    (tmp_path / filename).write_text("placeholder", encoding="utf-8")

    with pytest.raises(EvidenceAuditError, match="forbidden evidence file"):
        audit_evidence(tmp_path)


def test_symlink_and_oversized_files_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.log"
    target.write_text("safe", encoding="utf-8")
    link = tmp_path / "link.log"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this host")

    with pytest.raises(EvidenceAuditError, match="symbolic link"):
        audit_evidence(tmp_path)

    link.unlink()
    (tmp_path / "large.log").write_bytes(b"x" * 33)
    with pytest.raises(EvidenceAuditError, match="size limit"):
        audit_evidence(tmp_path, max_file_bytes=32)
