import io
import logging
from uuid import uuid4

from clients.booking_client import BookingClient, _redact_payload


class FakeResponse:
    def __init__(self, status_code: int = 201, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {"bookingid": 1}
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.calls: list[dict] = []
        self.response = response or FakeResponse()

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _memory_logger() -> logging.Logger:
    logger = logging.getLogger(f"test.booking_client.{uuid4()}")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def test_create_booking_posts_payload_to_booking_endpoint() -> None:
    session = FakeSession()
    client = BookingClient("https://example.test", timeout=5, session=session, logger=_memory_logger())
    payload = {"firstname": "Ada"}

    client.create_booking(payload)

    assert session.calls == [
        {
            "method": "POST",
            "url": "https://example.test/booking",
            "headers": {"Accept": "application/json", "Content-Type": "application/json"},
            "json": payload,
            "params": None,
            "timeout": 5,
        }
    ]


def test_update_booking_sends_auth_cookie() -> None:
    session = FakeSession()
    client = BookingClient("https://example.test", timeout=5, session=session, logger=_memory_logger())

    client.update_booking(7, {"firstname": "Ada"}, token="secret")

    assert session.calls[0]["url"] == "https://example.test/booking/7"
    assert session.calls[0]["headers"]["Cookie"] == "token=secret"


def test_redact_payload_recursively_hides_sensitive_values_without_mutating_input() -> None:
    payload = {
        "username": "admin",
        "password": "secret",
        "nested": {
            "Authorization": "Bearer secret",
            "items": [{"TOKEN": "abc", "cookie": "token=def", "Set-Cookie": "token=ghi"}],
        },
    }

    redacted = _redact_payload(payload)

    assert redacted == {
        "username": "admin",
        "password": "***",
        "nested": {
            "Authorization": "***",
            "items": [{"TOKEN": "***", "cookie": "***", "Set-Cookie": "***"}],
        },
    }
    assert payload["password"] == "secret"
    assert payload["nested"]["Authorization"] == "Bearer secret"


def test_create_token_logs_redacted_credentials_and_response_but_returns_raw_response() -> None:
    stream = io.StringIO()
    logger = logging.getLogger(f"test.booking_client.{uuid4()}")
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(stream))
    logger.setLevel(logging.INFO)
    logger.propagate = False
    response = FakeResponse(200, {"token": "raw-auth-token"})
    client = BookingClient("https://example.test", timeout=5, session=FakeSession(response), logger=logger)

    returned_response = client.create_token({"username": "admin", "password": "do-not-log"})
    logged = stream.getvalue()

    assert returned_response is response
    assert returned_response.json()["token"] == "raw-auth-token"
    assert "do-not-log" not in logged
    assert "raw-auth-token" not in logged
    assert '"token": "***"' in logged
