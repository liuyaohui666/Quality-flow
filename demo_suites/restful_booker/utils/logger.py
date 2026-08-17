"""Framework logger configuration."""

import logging
from pathlib import Path


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def get_logger(name: str, log_directory: Path | None = None) -> logging.Logger:
    """Return a logger that writes to both the console and framework.log."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        resolved_log_directory = log_directory or Path(__file__).parents[1] / "logs"
        resolved_log_directory.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter(LOG_FORMAT)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        file_handler = logging.FileHandler(resolved_log_directory / "framework.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
