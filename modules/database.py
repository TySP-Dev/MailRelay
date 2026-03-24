"""SQLite-backed deduplication and delivery-state tracking."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Iterable, Optional

DB_PATH = Path(__file__).parent.parent / "data" / "mailrelay.db"

# Delivery states
STATE_PENDING = "pending"       # MBOX generated, not yet downloaded
STATE_DELIVERED = "delivered"   # IMAP pushed or MBOX confirmed downloaded


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist."""
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id  TEXT PRIMARY KEY,
                state       TEXT NOT NULL DEFAULT 'delivered',
                mbox_path   TEXT,
                created_at  DATETIME DEFAULT (datetime('now')),
                updated_at  DATETIME DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_state ON messages(state)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def is_known(message_id: str) -> bool:
    """Return True if a message ID is already tracked (any state)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def filter_new(message_ids: Iterable[str]) -> list[str]:
    """Return only the IDs not yet in the database."""
    ids = list(message_ids)
    if not ids:
        return []
    with _db() as conn:
        placeholders = ",".join("?" * len(ids))
        known = {
            row[0]
            for row in conn.execute(
                f"SELECT message_id FROM messages WHERE message_id IN ({placeholders})",
                ids,
            )
        }
    return [mid for mid in ids if mid not in known]


def mark_pending(message_ids: Iterable[str], mbox_path: str) -> None:
    """Record message IDs as pending (MBOX created, not yet downloaded)."""
    with _db() as conn:
        conn.executemany(
            """
            INSERT INTO messages (message_id, state, mbox_path)
            VALUES (?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                state = excluded.state,
                mbox_path = excluded.mbox_path,
                updated_at = datetime('now')
            """,
            [(mid, STATE_PENDING, mbox_path) for mid in message_ids],
        )


def mark_delivered(message_ids: Iterable[str]) -> None:
    """Mark message IDs as fully delivered."""
    ids = list(message_ids)
    if not ids:
        return
    with _db() as conn:
        conn.executemany(
            """
            INSERT INTO messages (message_id, state)
            VALUES (?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                state = excluded.state,
                mbox_path = NULL,
                updated_at = datetime('now')
            """,
            [(mid, STATE_DELIVERED) for mid in ids],
        )


def get_pending_mboxes() -> list[dict]:
    """Return all distinct pending MBOX paths with their message IDs."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT message_id, mbox_path FROM messages WHERE state = ?",
            (STATE_PENDING,),
        ).fetchall()

    by_path: dict[str, list[str]] = {}
    for row in rows:
        path = row["mbox_path"]
        by_path.setdefault(path, []).append(row["message_id"])

    return [{"mbox_path": path, "message_ids": ids} for path, ids in by_path.items()]


def get_last_sync_time() -> Optional[datetime]:
    """Return the UTC timestamp of the last completed sync, or None."""
    with _db() as conn:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'last_sync_at'"
        ).fetchone()
    if not row:
        return None
    return datetime.fromisoformat(row["value"]).replace(tzinfo=timezone.utc)


def record_sync_time() -> None:
    """Record that a sync cycle just completed."""
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES ('last_sync_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (now,),
        )


def clear_pending_for_mbox(mbox_path: str) -> list[str]:
    """Remove pending state for a given MBOX (used on cleanup/re-process).

    Returns the list of message IDs that were pending for that MBOX.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT message_id FROM messages WHERE state = ? AND mbox_path = ?",
            (STATE_PENDING, mbox_path),
        ).fetchall()
        message_ids = [row["message_id"] for row in rows]
        conn.execute(
            "DELETE FROM messages WHERE state = ? AND mbox_path = ?",
            (STATE_PENDING, mbox_path),
        )
    return message_ids
