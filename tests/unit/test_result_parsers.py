from __future__ import annotations

from pathlib import Path

import pytest

from quality_flow.runners.parsers import ResultParseError, parse_junit_xml, parse_locust_stats


def test_parse_junit_handles_nested_suites_and_each_terminal_case_status(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """<?xml version=\"1.0\"?>
<testsuites tests=\"4\" failures=\"1\" errors=\"1\" skipped=\"1\">
  <testsuite name=\"outer\" tests=\"4\" failures=\"1\" errors=\"1\" skipped=\"1\">
    <testsuite name=\"nested\">
      <testcase classname=\"pkg.test_a\" name=\"test_pass\" time=\"0.125\" />
      <testcase classname=\"pkg.test_a\" name=\"test_fail\" time=\"0.5\"><failure message=\"assertion failed\" /></testcase>
      <testcase classname=\"pkg.test_b\" name=\"test_error\" time=\"1\"><error message=\"setup error\" /></testcase>
      <testcase classname=\"pkg.test_b\" name=\"test_skip\" time=\"0\"><skipped message=\"not applicable\" /></testcase>
    </testsuite>
  </testsuite>
</testsuites>""",
        encoding="utf-8",
    )

    result = parse_junit_xml(report)

    assert result.summary.total == 4
    assert (result.summary.passed, result.summary.failed, result.summary.errors, result.summary.skipped) == (1, 1, 1, 1)
    assert [(case.node_id, case.status, case.duration_ms) for case in result.cases] == [
        ("pkg.test_a::test_pass", "passed", 125.0),
        ("pkg.test_a::test_fail", "failed", 500.0),
        ("pkg.test_b::test_error", "error", 1000.0),
        ("pkg.test_b::test_skip", "skipped", 0.0),
    ]


@pytest.mark.parametrize(
    "xml",
    [
        "<testsuite><testcase name=\"a\" time=\"nan\" /></testsuite>",
        "<testsuite><testcase name=\"a\" time=\"-1\" /></testsuite>",
        "<testsuite tests=\"2\"><testcase name=\"a\" /></testsuite>",
        "<testsuite><testcase name=\"a\" /><testcase name=\"a\" /></testsuite>",
        "<!DOCTYPE x [<!ENTITY boom \"x\">]><testsuite><testcase name=\"&boom;\" /></testsuite>",
        "<testsuite><testcase name=\"a\"></testsuite>",
    ],
)
def test_parse_junit_rejects_unsafe_malformed_or_inconsistent_reports(
    tmp_path: Path, xml: str
) -> None:
    report = tmp_path / "bad.xml"
    report.write_text(xml, encoding="utf-8")

    with pytest.raises(ResultParseError):
        parse_junit_xml(report)


def test_parse_junit_rejects_utf16_entity_document(tmp_path: Path) -> None:
    entity_report = tmp_path / "entity.xml"
    entity_report.write_bytes(
        "<!DOCTYPE x [<!ENTITY boom 'expanded'>]><testsuite><testcase name='&boom;' /></testsuite>".encode(
            "utf-16"
        )
    )
    with pytest.raises(ResultParseError):
        parse_junit_xml(entity_report)


def test_parse_junit_rejects_conflicting_outcomes(tmp_path: Path) -> None:
    conflict_report = tmp_path / "conflict.xml"
    conflict_report.write_text(
        "<testsuite><testcase name='case'><failure /><skipped /></testcase></testsuite>",
        encoding="utf-8",
    )

    with pytest.raises(ResultParseError):
        parse_junit_xml(conflict_report)


def test_parse_locust_standard_aggregate_row(tmp_path: Path) -> None:
    stats = tmp_path / "stats.csv"
    stats.write_text(
        "Type,Name,Request Count,Failure Count,Average Response Time,Requests/s,95%\n"
        "GET,/health,10,1,20.5,2.5,30\n"
        "Aggregated,Aggregated,10,1,20.5,2.5,30\n",
        encoding="utf-8",
    )

    result = parse_locust_stats(stats)

    assert result.request_count == 10
    assert result.failure_ratio == 0.1
    assert result.requests_per_second == 2.5
    assert result.average_response_time_ms == 20.5
    assert result.p95_ms == 30.0


@pytest.mark.parametrize(
    "row",
    [
        "Aggregated,Aggregated,,0,1,1,1",
        "Aggregated,Aggregated,1,-1,1,1,1",
        "Aggregated,Aggregated,1,2,1,1,1",
        "Aggregated,Aggregated,1,0,nan,1,1",
        "Aggregated,Aggregated,1,0,1,inf,1",
    ],
)
def test_parse_locust_rejects_invalid_aggregate_values(tmp_path: Path, row: str) -> None:
    stats = tmp_path / "stats.csv"
    stats.write_text(
        "Type,Name,Request Count,Failure Count,Average Response Time,Requests/s,95%\n" + row + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ResultParseError):
        parse_locust_stats(stats)


def test_parse_locust_rejects_missing_aggregate_row(tmp_path: Path) -> None:
    stats = tmp_path / "stats.csv"
    stats.write_text(
        "Type,Name,Request Count,Failure Count,Average Response Time,Requests/s,95%\nGET,/health,1,0,1,1,1\n",
        encoding="utf-8",
    )

    with pytest.raises(ResultParseError):
        parse_locust_stats(stats)


@pytest.mark.parametrize(
    "content",
    [
        b"Type,Name,Request Count,Failure Count,Average Response Time,Requests/s,95%\nAggregated,Aggregated,1,0,1,1,1\n\"unterminated",
        b"Type,Name,Request Count,Failure Count,Average Response Time,Requests/s,95%\nAggregated,Aggregated,1,0,1,1,1\xff\n",
    ],
)
def test_parse_locust_wraps_malformed_csv_and_encoding_errors(tmp_path: Path, content: bytes) -> None:
    stats = tmp_path / "bad.csv"
    stats.write_bytes(content)

    with pytest.raises(ResultParseError):
        parse_locust_stats(stats)
