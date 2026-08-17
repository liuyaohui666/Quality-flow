import io
import logging
from uuid import uuid4

from clients.booking_client import BookingClient, _redact_payload


class FakeResponse:
    def __init__(self, status_code: int = 201, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = {"bookingid": 1} if payload is None else payload
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


class NonJsonFakeResponse:
    status_code = 502
    text = (
        "token=token-sentinel authorization=authorization-sentinel "
        "cookie=cookie-sentinel set-cookie=set-cookie-sentinel"
    )

    def json(self) -> dict:
        raise ValueError("not JSON")


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


def test_fake_response_preserves_an_explicit_empty_payload() -> None:
    response = FakeResponse(200, {})

    assert response.json() == {}
    assert response.text == "{}"


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


def test_non_json_response_never_logs_raw_sensitive_text_to_stream_or_file(tmp_path) -> None:
    stream = io.StringIO()
    logger = logging.getLogger(f"test.booking_client.{uuid4()}")
    logger.handlers.clear()
    stream_handler = logging.StreamHandler(stream)
    file_path = tmp_path / "booking-client.log"
    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    client = BookingClient(
        "https://example.test",
        timeout=5,
        session=FakeSession(NonJsonFakeResponse()),
        logger=logger,
    )

    client.get_booking(7)
    for handler in logger.handlers:
        handler.flush()

    stream_content = stream.getvalue()
    file_content = file_path.read_text(encoding="utf-8")
    for sentinel in (
        "token-sentinel",
        "authorization-sentinel",
        "cookie-sentinel",
        "set-cookie-sentinel",
    ):
        assert sentinel not in stream_content
        assert sentinel not in file_content
