"""Push emails to iCloud Mail via IMAP APPEND.

iCloud IMAP settings:
  Host : imap.mail.me.com
  Port : 993 (SSL)
  Auth : email + app-specific password

The APPEND command places a message directly into a mailbox without going
through SMTP, so no "sent" copy is created and delivery is instant.
"""

import imaplib
import socket
from typing import Optional

from .database import mark_delivered
from .logger import get_logger
from .processor import RichEmail

log = get_logger(__name__)

ICLOUD_IMAP_HOST = "imap.mail.me.com"
ICLOUD_IMAP_PORT = 993
DEFAULT_MAILBOX = "INBOX"
CONNECT_TIMEOUT = 30  # seconds


class ForwarderError(Exception):
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def push_emails(
    emails: list[RichEmail],
    icloud_email: str,
    icloud_password: str,
    mailbox: str = DEFAULT_MAILBOX,
) -> tuple[list[str], list[str]]:
    """APPEND each email to the iCloud mailbox.

    Returns (succeeded_ids, failed_ids).
    Records succeeded IDs as delivered in SQLite.
    """
    if not emails:
        return [], []

    succeeded: list[str] = []
    failed: list[str] = []

    try:
        conn = _connect(icloud_email, icloud_password)
    except ForwarderError as exc:
        log.error("IMAP connection failed: %s", exc)
        return [], [e.message_id for e in emails]

    try:
        for rich in emails:
            try:
                _append(conn, rich, mailbox)
                succeeded.append(rich.message_id)
                log.info("IMAP pushed: %s", rich.message_id)
            except Exception as exc:
                log.error("Failed to push %s: %s", rich.message_id, exc)
                failed.append(rich.message_id)
    finally:
        _logout(conn)

    if succeeded:
        mark_delivered(succeeded)
        log.info("Marked %d message(s) as delivered.", len(succeeded))

    return succeeded, failed


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _connect(email_addr: str, password: str) -> imaplib.IMAP4_SSL:
    log.debug("Connecting to %s:%d …", ICLOUD_IMAP_HOST, ICLOUD_IMAP_PORT)
    try:
        conn = imaplib.IMAP4_SSL(
            ICLOUD_IMAP_HOST,
            ICLOUD_IMAP_PORT,
        )
    except (OSError, socket.gaierror) as exc:
        raise ForwarderError(f"Cannot reach {ICLOUD_IMAP_HOST}: {exc}") from exc

    try:
        conn.login(email_addr, password)
        log.debug("IMAP login successful for %s", email_addr)
    except imaplib.IMAP4.error as exc:
        raise ForwarderError(f"IMAP authentication failed: {exc}") from exc

    return conn


def _append(
    conn: imaplib.IMAP4_SSL,
    rich: RichEmail,
    mailbox: str,
) -> None:
    """APPEND a single message to *mailbox*."""
    # imaplib.IMAP4.append expects: mailbox, flags, date_time, message
    # We pass None for flags and date_time to let the server set defaults.
    status, data = conn.append(mailbox, None, None, rich.raw_bytes)
    if status != "OK":
        raise ForwarderError(
            f"APPEND returned {status} for {rich.message_id}: {data}"
        )


def _logout(conn: imaplib.IMAP4_SSL) -> None:
    try:
        conn.logout()
    except Exception:
        pass
