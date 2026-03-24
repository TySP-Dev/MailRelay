"""Rotating log file and console setup for MailRelay.

Two modes:
  normal  — console shows only user-facing INFO messages (sync, push, MBOX)
            plus WARNING/ERROR from any logger
  debug   — console shows DEBUG from every logger with full context
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "data" / "mailrelay.log"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 3

_NORMAL_FORMAT = "%(asctime)s [%(levelname)-8s] %(message)s"
_DEBUG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s.%(funcName)s:%(lineno)d — %(message)s"

# Loggers whose INFO messages are shown in normal (non-debug) mode
_USER_LOGGERS = frozenset({
    "__main__",
    "modules.exporter",
    "modules.forwarder",
    "modules.packager",
})

# Third-party loggers silenced to WARNING in all modes
_SUPPRESS = ("apscheduler", "uvicorn", "fastapi", "asyncio", "multipart", "starlette")


class _UserFacingFilter(logging.Filter):
    """Pass WARNING+ from any logger; INFO only from the user-visible set."""
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return record.name in _USER_LOGGERS


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that propagates to the root handler."""
    return logging.getLogger(name)


def configure_logging(debug: bool = False) -> None:
    """Set up root logger with file + console handlers. Call once at startup."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    # File handler — always full DEBUG detail
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        LOG_PATH, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_DEBUG_FORMAT))
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    if debug:
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(logging.Formatter(_DEBUG_FORMAT))
    else:
        ch.setLevel(logging.INFO)
        ch.addFilter(_UserFacingFilter())
        ch.setFormatter(logging.Formatter(_NORMAL_FORMAT))
    root.addHandler(ch)

    # Silence noisy third-party loggers
    for lib in _SUPPRESS:
        logging.getLogger(lib).setLevel(logging.WARNING)


def tail_log(lines: int = 50) -> str:
    """Return the last N lines of the log file as a string."""
    if not LOG_PATH.exists():
        return "(no log file yet)"
    text = LOG_PATH.read_text(encoding="utf-8")
    return "\n".join(text.splitlines()[-lines:])
