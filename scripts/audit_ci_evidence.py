"""Reject unsafe files before CI diagnostic evidence is uploaded."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
from typing import Iterable, Sequence


ALLOWED_SUFFIXES = {".json", ".log", ".txt", ".xml"}
FORBIDDEN_NAMES = {".env"}
FORBIDDEN_SUFFIXES = {".db", ".dump", ".sql", ".sqlite", ".sqlite3"}
SENSITIVE_TEXT = (
    re.compile(r"(?im)^\s*(?:authorization|cookie|set-cookie)\s*:"),
    re.compile(
        r"(?i)\b(?:https?|postgres(?:ql)?(?:\+[a-z0-9_]+)?|redis)://"
        r"[^\s/@:]+:[^\s/@]+@"
    ),
)


class EvidenceAuditError(RuntimeError):
    """Raised with a redacted reason when retained evidence is unsafe."""


def _reject(path: Path, reason: str) -> None:
    raise EvidenceAuditError(f"{reason}: {path.name}")


def audit_evidence(
    root: Path,
    *,
    forbidden_values: Iterable[str] = (),
    max_file_bytes: int = 5 * 1024 * 1024,
) -> int:
    """Audit one explicit evidence directory and return its regular-file count."""
    root = Path(root)
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive")
    if root.is_symlink():
        _reject(root, "evidence root cannot be a symbolic link")
    if not root.is_dir():
        raise EvidenceAuditError("evidence root is unavailable")

    secrets = tuple(value.encode("utf-8") for value in forbidden_values if value)
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            _reject(path, "symbolic link is not allowed")
        if path.is_dir():
            continue
        if not path.is_file():
            _reject(path, "unsupported evidence entry")
        if path.name.lower() in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            _reject(path, "forbidden evidence file")
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            _reject(path, "evidence file type is not allowlisted")
        if path.stat().st_size > max_file_bytes:
            _reject(path, "evidence file exceeds size limit")

        content = path.read_bytes()
        if any(secret in content for secret in secrets):
            _reject(path, "forbidden value found in evidence")
        text = content.decode("utf-8", errors="replace")
        if any(pattern.search(text) for pattern in SENSITIVE_TEXT):
            _reject(path, "credential-like text found in evidence")
        count += 1
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--max-file-bytes", type=int, default=5 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        count = audit_evidence(
            args.root,
            forbidden_values=(os.environ.get("QUALITY_FLOW_CI_SECRET_CANARY", ""),),
            max_file_bytes=args.max_file_bytes,
        )
    except (EvidenceAuditError, OSError, ValueError) as error:
        print(f"evidence audit failed: {error}")
        return 1
    print(f"evidence audit passed: {count} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
