"""Strict, file-based parsers for trusted runner result formats."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Iterator
from xml.etree import ElementTree as ElementTree

from quality_flow.runners.base import CaseResultData, CaseSummary, PerformanceSummary


class ResultParseError(ValueError):
    """A runner result was absent, malformed, unsafe, or internally inconsistent."""


@dataclass(frozen=True)
class JUnitParseResult:
    cases: tuple[CaseResultData, ...]
    summary: CaseSummary

    @property
    def case_results(self) -> tuple[CaseResultData, ...]:
        return self.cases

    def __iter__(self) -> Iterator[object]:
        """Allow ``cases, summary = parse_junit_xml(...)`` for simple consumers."""
        yield self.cases
        yield self.summary


def parse_junit_xml(path: Path) -> JUnitParseResult:
    """Parse a JUnit XML report without accepting DTD/entity-bearing documents."""
    xml_bytes = _read_bytes(path, "JUnit XML")
    if re.search(
        r"<!\s*(?:DOCTYPE|ENTITY)\b", _xml_security_text(xml_bytes), re.IGNORECASE
    ):
        raise ResultParseError("JUnit XML must not contain DTDs or entities")
    try:
        root = ElementTree.fromstring(xml_bytes)
    except (ElementTree.ParseError, UnicodeError) as error:
        raise ResultParseError("JUnit XML is malformed") from error
    if _tag_name(root.tag) not in {"testsuite", "testsuites"}:
        raise ResultParseError("JUnit XML root must be testsuite or testsuites")

    cases: list[CaseResultData] = []
    seen_node_ids: set[str] = set()
    for case_element in root.iter():
        if _tag_name(case_element.tag) != "testcase":
            continue
        case = _parse_case(case_element)
        if case.node_id in seen_node_ids:
            raise ResultParseError(f"JUnit XML has duplicate node_id: {case.node_id}")
        seen_node_ids.add(case.node_id)
        cases.append(case)

    summary = _case_summary(cases)
    _validate_suite_summaries(root, summary)
    return JUnitParseResult(cases=tuple(cases), summary=summary)


def parse_locust_stats(path: Path) -> PerformanceSummary:
    """Parse the required aggregate row from a standard Locust stats CSV."""
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file, strict=True)
            if not reader.fieldnames:
                raise ResultParseError("Locust CSV has no header")
            columns = _locust_columns(reader.fieldnames)
            aggregate: dict[str, str | None] | None = None
            for row in reader:
                if None in row:
                    raise ResultParseError("Locust CSV has too many fields in a row")
                if _is_aggregate_row(row, columns):
                    if aggregate is not None:
                        raise ResultParseError("Locust CSV has more than one aggregate row")
                    aggregate = row
    except (OSError, UnicodeError, csv.Error) as error:
        raise ResultParseError("Locust CSV is malformed or unreadable") from error
    if aggregate is None:
        raise ResultParseError("Locust CSV has no aggregate row")

    request_count = _non_negative_int(_field(aggregate, columns, "request_count"), "request count")
    failure_count = _non_negative_int(_field(aggregate, columns, "failure_count"), "failure count")
    if failure_count > request_count:
        raise ResultParseError("Locust failure count cannot exceed request count")
    average_ms = _non_negative_float(_field(aggregate, columns, "average_ms"), "average response time")
    requests_per_second = _non_negative_float(
        _field(aggregate, columns, "requests_per_second"), "requests per second"
    )
    p95_ms = _non_negative_float(_field(aggregate, columns, "p95_ms"), "p95 response time")
    return PerformanceSummary(
        request_count=request_count,
        p95_ms=p95_ms,
        failure_ratio=(failure_count / request_count if request_count else 0.0),
        requests_per_second=requests_per_second,
        average_response_time_ms=average_ms,
        failure_count=failure_count,
    )


def _read_bytes(path: Path, format_name: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise ResultParseError(f"{format_name} could not be read") from error


def _xml_security_text(xml_bytes: bytes) -> str:
    """Decode enough XML encodings to make a DTD/entity preflight meaningful."""
    encoding = "utf-8"
    if xml_bytes.startswith(b"\xff\xfe\x00\x00"):
        encoding = "utf-32-le"
    elif xml_bytes.startswith(b"\x00\x00\xfe\xff"):
        encoding = "utf-32-be"
    elif xml_bytes.startswith(b"\xff\xfe"):
        encoding = "utf-16-le"
    elif xml_bytes.startswith(b"\xfe\xff"):
        encoding = "utf-16-be"
    elif xml_bytes.startswith(b"<\x00\x00\x00"):
        encoding = "utf-32-le"
    elif xml_bytes.startswith(b"\x00\x00\x00<"):
        encoding = "utf-32-be"
    elif xml_bytes.startswith(b"<\x00"):
        encoding = "utf-16-le"
    elif xml_bytes.startswith(b"\x00<"):
        encoding = "utf-16-be"
    try:
        return xml_bytes.decode(encoding)
    except UnicodeDecodeError as error:
        raise ResultParseError("JUnit XML has an invalid encoding") from error


def _tag_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _parse_case(element: ElementTree.Element[str]) -> CaseResultData:
    name = element.get("name")
    if not name:
        raise ResultParseError("JUnit testcase is missing a name")
    classname = element.get("classname", "")
    node_id = f"{classname}::{name}" if classname else name
    duration_seconds = _non_negative_float(element.get("time", "0"), "JUnit testcase time")
    child_elements = list(element)
    status, message = "passed", None
    outcome_children = [
        child
        for child in child_elements
        if _tag_name(child.tag) in {"error", "failure", "skipped"}
    ]
    if len(outcome_children) > 1:
        raise ResultParseError("JUnit testcase has conflicting terminal outcomes")
    if outcome_children:
        result_element = outcome_children[0]
        status = {
            "error": "error",
            "failure": "failed",
            "skipped": "skipped",
        }[_tag_name(result_element.tag)]
        message = result_element.get("message") or _element_text(result_element)
    return CaseResultData(
        node_id=node_id,
        status=status,
        duration_ms=duration_seconds * 1000,
        message=message,
    )


def _element_text(element: ElementTree.Element[str]) -> str | None:
    text = "".join(element.itertext()).strip()
    return text or None


def _case_summary(cases: list[CaseResultData]) -> CaseSummary:
    return CaseSummary(
        total=len(cases),
        passed=sum(case.status == "passed" for case in cases),
        failed=sum(case.status == "failed" for case in cases),
        errors=sum(case.status == "error" for case in cases),
        skipped=sum(case.status == "skipped" for case in cases),
    )


def _validate_suite_summaries(root: ElementTree.Element[str], summary: CaseSummary) -> None:
    for suite in root.iter():
        if _tag_name(suite.tag) not in {"testsuite", "testsuites"}:
            continue
        descendant_summary = _case_summary(
            [_parse_case(case) for case in suite.iter() if _tag_name(case.tag) == "testcase"]
        )
        for attribute, actual in (
            ("tests", descendant_summary.total),
            ("failures", descendant_summary.failed),
            ("errors", descendant_summary.errors),
            ("skipped", descendant_summary.skipped),
        ):
            if attribute not in suite.attrib:
                continue
            declared = _non_negative_int(suite.attrib[attribute], f"JUnit {attribute}")
            if declared != actual:
                raise ResultParseError(
                    f"JUnit {attribute} summary conflicts with testcase results"
                )
    if _tag_name(root.tag) == "testsuites" and "tests" in root.attrib:
        if summary.total != _non_negative_int(root.attrib["tests"], "JUnit tests"):
            raise ResultParseError("JUnit root summary conflicts with testcase results")


def _locust_columns(fieldnames: list[str]) -> dict[str, str]:
    by_normalized: dict[str, str] = {}
    for header in fieldnames:
        normalized = _normalize_header(header)
        if normalized in by_normalized:
            raise ResultParseError("Locust CSV has duplicate normalized columns")
        by_normalized[normalized] = header
    aliases = {
        "type": ("type",),
        "name": ("name",),
        "request_count": ("requestcount",),
        "failure_count": ("failurecount",),
        "average_ms": ("averageresponsetime",),
        "requests_per_second": ("requestss", "requestspersecond"),
        "p95_ms": ("95", "95responsetime", "p95", "p95responsetime"),
    }
    selected: dict[str, str] = {}
    for name, choices in aliases.items():
        column = next((by_normalized[choice] for choice in choices if choice in by_normalized), None)
        if column is None:
            raise ResultParseError(f"Locust CSV is missing {name} column")
        selected[name] = column
    return selected


def _normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.casefold())


def _is_aggregate_row(row: dict[str, str | None], columns: dict[str, str]) -> bool:
    return any(
        (row.get(columns[key]) or "").strip().casefold() == "aggregated"
        for key in ("type", "name")
    )


def _field(row: dict[str, str | None], columns: dict[str, str], name: str) -> str | None:
    return row.get(columns[name])


def _non_negative_int(value: str | None, field_name: str) -> int:
    if value is None or not value.strip() or not re.fullmatch(r"[0-9]+", value.strip()):
        raise ResultParseError(f"{field_name} must be a non-negative integer")
    return int(value)


def _non_negative_float(value: str | None, field_name: str) -> float:
    if value is None or not value.strip():
        raise ResultParseError(f"{field_name} must be a finite non-negative number")
    try:
        result = float(value)
    except ValueError as error:
        raise ResultParseError(f"{field_name} must be a finite non-negative number") from error
    if not math.isfinite(result) or result < 0:
        raise ResultParseError(f"{field_name} must be a finite non-negative number")
    return result
