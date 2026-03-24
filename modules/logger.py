"""Rotating log file setup for MailRelay."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "data" / "mailrelay.log"
LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s.%(funcName)s:%(lineno)d — %(message)s"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 3


def get_logger(name: str) -> logging.Logger:
    """Return a named logger wired to the shared rotating file + stderr."""
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.DEBUG)

    # Rotating file handler
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    # Console handler (all levels)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def tail_log(lines: int = 50) -> str:
    """Return the last N lines of the log file as a string."""
    if not LOG_PATH.exists():
        return "(no log file yet)"
    text = LOG_PATH.read_text(encoding="utf-8")
    all_lines = text.splitlines()
    return "\n".join(all_lines[-lines:])
