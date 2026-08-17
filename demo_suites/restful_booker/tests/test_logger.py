import logging
from uuid import uuid4

from utils.logger import get_logger


def test_get_logger_writes_info_message_to_log_file(tmp_path) -> None:
    logger = get_logger(f"test.logger.{uuid4()}", log_directory=tmp_path)

    logger.info("Booking request completed")
    for handler in logger.handlers:
        handler.flush()

    log_content = (tmp_path / "framework.log").read_text(encoding="utf-8")
    assert "INFO" in log_content
    assert "Booking request completed" in log_content
    assert any(type(handler) is logging.StreamHandler for handler in logger.handlers)
    assert any(isinstance(handler, logging.FileHandler) for handler in logger.handlers)
