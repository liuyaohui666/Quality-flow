"""Wait for one QualityFlow Run through the public API only."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
import time
from typing import Any, Protocol
from uuid import UUID

import httpx


TERMINAL_STATUSES = {"completed", "infra_failed", "timed_out"}


class ApiClient(Protocol):
    def get(self, path: str) -> Any: ...


class WaitTimeout(TimeoutError):
    def __init__(self, run_id: str, last_observed: dict[str, Any] | None) -> None:
        self.run_id = run_id
        self.last_observed = last_observed
        super().__init__(
            json.dumps(
                {"error": "deadline_exceeded", "run_id": run_id, "last_observed": last_observed},
                sort_keys=True,
            )
        )


def wait_for_terminal(
    client: ApiClient,
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if timeout_seconds <= 0 or poll_interval <= 0:
        raise ValueError("timeout and poll interval must be positive")
    deadline = monotonic() + timeout_seconds
    last_observed: dict[str, Any] | None = None
    while (remaining := deadline - monotonic()) > 0:
        response = client.get(f"/api/v1/runs/{run_id}")
        response.raise_for_status()
        last_observed = response.json()
        if last_observed.get("status") in TERMINAL_STATUSES:
            return last_observed
        sleep(min(poll_interval, remaining))
    raise WaitTimeout(run_id, last_observed)


def terminal_exit_code(run: dict[str, Any]) -> int:
    return 0 if (run.get("status"), run.get("outcome")) == ("completed", "passed") else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=lambda value: str(UUID(value)))
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:18000",
        help="QualityFlow API base URL",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args(argv)
    try:
        with httpx.Client(base_url=args.api_url, timeout=10.0, trust_env=False) as client:
            run = wait_for_terminal(
                client,
                args.run_id,
                timeout_seconds=args.timeout,
                poll_interval=args.poll_interval,
            )
    except WaitTimeout as error:
        print(str(error))
        return 2
    except (httpx.HTTPError, ValueError) as error:
        print(json.dumps({"error": type(error).__name__, "detail": str(error)}))
        return 3
    print(json.dumps(run, sort_keys=True))
    return terminal_exit_code(run)


if __name__ == "__main__":
    raise SystemExit(main())
