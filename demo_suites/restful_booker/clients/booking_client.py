"""HTTP client for Restful Booker booking and authentication endpoints."""

import logging
from typing import Any

import requests

from utils.logger import get_logger
from utils.sanitization import (
    sanitized_json,
    sanitized_response_body,
)


class BookingClient:
    """Encapsulate Restful Booker HTTP details behind business-level methods."""

    def __init__(
        self,
        base_url: str,
        timeout: int,
        session: requests.Session | Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.logger = logger or get_logger(self.__class__.__name__)

    def create_token(self, credentials: dict[str, str]):
        return self._request("POST", "/auth", json=credentials)

    def create_booking(self, booking: dict[str, Any]):
        return self._request("POST", "/booking", json=booking)

    def list_bookings(self, params: dict[str, str] | None = None):
        return self._request("GET", "/booking", params=params)

    def get_booking(self, booking_id: int):
        return self._request("GET", f"/booking/{booking_id}")

    def update_booking(self, booking_id: int, booking: dict[str, Any], token: str):
        return self._request("PUT", f"/booking/{booking_id}", json=booking, token=token)

    def partial_update_booking(self, booking_id: int, fields: dict[str, Any], token: str):
        return self._request("PATCH", f"/booking/{booking_id}", json=fields, token=token)

    def delete_booking(self, booking_id: int, token: str):
        return self._request("DELETE", f"/booking/{booking_id}", token=token)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        token: str | None = None,
    ):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Cookie"] = f"token={token}"
        url = f"{self.base_url}{path}"
        self.logger.info(
            "%s %s | request=%s",
            method,
            url,
            sanitized_json({"headers": headers, "params": params, "body": json}),
        )
        response = self.session.request(
            method=method,
            url=url,
            headers=headers,
            json=json,
            params=params,
            timeout=self.timeout,
        )
        self.logger.info(
            "Response %s | body=%s",
            response.status_code,
            sanitized_response_body(response),
        )
        return response
