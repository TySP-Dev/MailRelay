"""MBOX packaging and local HTTP download server (Mode 2 / fallback).

Flow:
  1. bundle_emails()   — write a timestamped .mbox into data/downloads/
                         mark message IDs as "pending" in SQLite
                         return the local download URL
  2. start_server()    — launch a FastAPI/uvicorn server in a background thread
                         serving data/downloads/
  3. On GET /download/{filename} the server streams the file, then marks all
     IDs for that MBOX as delivered and deletes the file.
  4. cleanup_stale()   — called at the start of each sync cycle; deletes any
                         MBOX files that were never collected and removes their
                         pending DB entries so they can be re-processed.
"""

import mailbox
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import database
from .logger import get_logger
from .processor import RichEmail

log = get_logger(__name__)

DOWNLOADS_DIR = Path(__file__).parent.parent / "data" / "downloads"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8765

_server_thread: Optional[threading.Thread] = None
_uvicorn_server: Optional[uvicorn.Server] = None

app = FastAPI(title="MailRelay Download Server", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def bundle_emails(emails: list[RichEmail]) -> Optional[str]:
    """Write emails to a new .mbox file and return the download URL.

    Returns None if emails list is empty.
    Marks message IDs as 'pending' in SQLite.
    """
    if not emails:
        return None

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    mbox_filename = f"mailrelay_{timestamp}.mbox"
    mbox_path = DOWNLOADS_DIR / mbox_filename

    mbox = mailbox.mbox(str(mbox_path))
    mbox.lock()
    try:
        for rich in emails:
            mbox.add(mailbox.mboxMessage(rich.raw_bytes))
    finally:
        mbox.flush()
        mbox.unlock()
        mbox.close()

    message_ids = [e.message_id for e in emails]
    database.mark_pending(message_ids, str(mbox_path))

    url = f"http://{SERVER_HOST}:{SERVER_PORT}/download/{mbox_filename}"
    log.info(
        "MBOX bundle created: %s (%d message(s)). Download: %s",
        mbox_filename,
        len(emails),
        url,
    )
    return url


def cleanup_stale() -> list[str]:
    """Remove MBOX files from previous cycles that were never downloaded.

    Returns the list of message IDs that were cleared (will be re-processed
    on the next sync since they are removed from the DB).
    """
    pending = database.get_pending_mboxes()
    cleared_ids: list[str] = []

    for entry in pending:
        mbox_path = Path(entry["mbox_path"])
        ids = entry["message_ids"]

        log.warning(
            "Stale MBOX detected (never downloaded): %s — clearing %d pending ID(s) for re-processing.",
            mbox_path.name,
            len(ids),
        )
        cleared = database.clear_pending_for_mbox(str(mbox_path))
        cleared_ids.extend(cleared)

        if mbox_path.exists():
            try:
                mbox_path.unlink()
                log.info("Deleted stale MBOX: %s", mbox_path.name)
            except OSError as exc:
                log.error("Could not delete %s: %s", mbox_path.name, exc)

    return cleared_ids


def start_server() -> None:
    """Start the FastAPI download server in a daemon thread (idempotent)."""
    global _server_thread, _uvicorn_server

    if _server_thread and _server_thread.is_alive():
        return  # already running

    config = uvicorn.Config(
        app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level="warning",
        access_log=False,
    )
    _uvicorn_server = uvicorn.Server(config)

    _server_thread = threading.Thread(
        target=_uvicorn_server.run, daemon=True, name="mailrelay-download-server"
    )
    _server_thread.start()
    log.info("Download server started at http://%s:%d", SERVER_HOST, SERVER_PORT)


def stop_server() -> None:
    """Gracefully stop the download server."""
    global _uvicorn_server
    if _uvicorn_server:
        _uvicorn_server.should_exit = True
        log.info("Download server stopped.")


# ---------------------------------------------------------------------------
# FastAPI routes
# ---------------------------------------------------------------------------

@app.get("/download/{filename}")
async def download_mbox(filename: str):
    """Serve a .mbox file, mark it delivered, then delete it."""
    # Basic path safety — no traversal
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    mbox_path = DOWNLOADS_DIR / filename
    if not mbox_path.exists():
        raise HTTPException(status_code=404, detail="File not found or already downloaded.")

    # We need to deliver after the response is sent.
    # Use a background task via starlette.
    from starlette.background import BackgroundTask

    task = BackgroundTask(_on_download_complete, str(mbox_path))
    log.info("Serving MBOX download: %s", filename)
    return FileResponse(
        path=str(mbox_path),
        media_type="application/mbox",
        filename=filename,
        background=task,
    )


@app.get("/status")
async def server_status():
    pending = database.get_pending_mboxes()
    return {
        "pending_mboxes": [
            {"file": Path(e["mbox_path"]).name, "message_count": len(e["message_ids"])}
            for e in pending
        ]
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _on_download_complete(mbox_path_str: str) -> None:
    """Called after a successful file download: mark delivered + delete file."""
    mbox_path = Path(mbox_path_str)
    ids = database.clear_pending_for_mbox(mbox_path_str)
    if ids:
        database.mark_delivered(ids)
        log.info(
            "Download confirmed for %s — marked %d message(s) as delivered.",
            mbox_path.name,
            len(ids),
        )
    if mbox_path.exists():
        try:
            mbox_path.unlink()
            log.info("Deleted downloaded MBOX: %s", mbox_path.name)
        except OSError as exc:
            log.error("Could not delete %s: %s", mbox_path.name, exc)
