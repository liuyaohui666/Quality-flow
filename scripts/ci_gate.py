"""Submit a registered suite and turn its terminal result into a CI exit code."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import httpx


Request = Callable[..., httpx.Response]


def _endpoint(api_url: str, path: str) -> str:
    parsed = urlsplit(api_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("api_url must be an absolute HTTP URL without query or fragment")
    joined_path = f"{parsed.path.rstrip('/')}/{path.lstrip('/')}"
    return urlunsplit((parsed.scheme, parsed.netloc, joined_path, "", ""))


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def run_ci_gate(
    api_url: str,
    suite_id: str,
    scenario: str,
    poll_interval_seconds: float,
    timeout_seconds: float,
    *,
    request: Request = httpx.request,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Return zero only when the control plane reports completed/passed."""
    if poll_interval_seconds <= 0 or timeout_seconds < 0:
        return 2
    deadline = clock() + timeout_seconds
    remaining = deadline - clock()
    if remaining <= 0:
        return 1
    try:
        submitted = request(
            "POST",
            _endpoint(api_url, "/api/v1/runs"),
            headers={"Idempotency-Key": f"ci-{uuid4()}"},
            json={"suite_id": suite_id, "parameters": {"scenario": scenario}},
            timeout=min(10, remaining),
        )
    except (httpx.HTTPError, ValueError):
        return 2
    if clock() >= deadline:
        return 1
    if submitted.status_code != 202:
        return 2
    payload = _json(submitted)
    if payload is None or not isinstance(payload.get("run_id"), str):
        return 2

    run_path = f"/api/v1/runs/{quote(payload['run_id'], safe='')}"
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return 1
        try:
            polled = request(
                "GET", _endpoint(api_url, run_path), timeout=min(10, remaining)
            )
        except (httpx.HTTPError, ValueError):
            return 2
        if clock() >= deadline:
            return 1
        if polled.status_code != 200:
            return 2
        result = _json(polled)
        if result is None:
            return 2
        status_value = result.get("status")
        outcome = result.get("outcome")
        if status_value == "completed":
            return 0 if outcome == "passed" else 1
        if status_value in {"infra_failed", "timed_out"}:
            return 1
        remaining = deadline - clock()
        if remaining <= 0:
            return 1
        sleep(min(poll_interval_seconds, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--suite-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    return run_ci_gate(
        args.api_url,
        args.suite_id,
        args.scenario,
        args.poll_interval,
        args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
