"""Automate proton-mail-export-cli via pexpect.

The Proton Export CLI interactive prompt sequence (observed order):
  1. Email address
  2. Password
  3. Two-factor authentication code   (only if 2FA is enabled)
  4. Mailbox password                  (only if separate mailbox password is set)

After authentication the CLI exports all mail to the export directory.
Each message produces two files:
  {messageID}.eml
  {messageID}.metadata.json

Because the exact prompt strings can vary between CLI versions and account
configurations, each pattern is defined as a regex so minor wording differences
are tolerated.  If the CLI changes its prompt wording, update PROMPTS below.
"""

from pathlib import Path
from typing import Optional

import pexpect

from .logger import get_logger
from .tools import BINARY_PATH as CLI_BINARY_PATH, ensure_export_cli

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configurable paths
# ---------------------------------------------------------------------------

EXPORT_DIR = Path(__file__).parent.parent / "data" / "exports"

# ---------------------------------------------------------------------------
# Prompt patterns (case-insensitive regex matched against CLI output)
# ---------------------------------------------------------------------------

PROMPTS = {
    "email":            r"[Ee]mail\s*(address)?[\s:>]+",
    "password":         r"[Pp]assword[\s:>]+",
    "totp":             r"[Tt]wo.factor|[Oo]ne.time|[Tt][Oo][Tt][Pp]|[Aa]uth.*code",
    "mailbox_password": r"[Mm]ailbox\s*[Pp]assword[\s:>]+",
    "done":             r"[Ee]xport\s*(complete|finished|done)|[Ss]uccessfully\s*export",
    "error":            r"[Ee]rror|[Ff]ailed|[Ii]nvalid",
}

# Maximum time to wait for each prompt (seconds)
PROMPT_TIMEOUT = 120
# Total export timeout — large mailboxes can take a while
EXPORT_TIMEOUT = 3600


class ExportError(Exception):
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_export(
    email: str,
    password: str,
    totp_code: str,
    mailbox_password: str = "",
    export_dir: Optional[Path] = None,
) -> Path:
    """Drive proton-mail-export-cli and return the export directory path.

    Raises ExportError on authentication failure or unexpected CLI output.
    """
    out_dir = export_dir or EXPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Starting Proton Mail export...")

    try:
        binary = ensure_export_cli()
    except Exception as exc:
        raise ExportError(str(exc)) from exc

    cmd = [str(binary), "--export-dir", str(out_dir)]
    log.info("Starting Proton export CLI: %s", " ".join(cmd))

    child = pexpect.spawn(
        cmd[0], cmd[1:], timeout=PROMPT_TIMEOUT, encoding="utf-8"
    )
    child.logfile_read = _PexpectLogger(log)

    try:
        _drive_cli(child, email, password, totp_code, mailbox_password)
    except pexpect.TIMEOUT as exc:
        child.close(force=True)
        raise ExportError("Timed out waiting for CLI prompt.") from exc
    except pexpect.EOF as exc:
        output = child.before or ""
        child.close()
        if child.exitstatus and child.exitstatus != 0:
            raise ExportError(
                f"CLI exited with code {child.exitstatus}. Output: {output.strip()}"
            ) from exc
        # EOF after a successful export is normal
        log.info("CLI process finished (EOF).")

    log.info("Export complete. Files in: %s", out_dir)
    return out_dir


def _drive_cli(
    child: pexpect.spawn,
    email: str,
    password: str,
    totp_code: str,
    mailbox_password: str,
) -> None:
    """Respond to each interactive prompt in sequence."""
    patterns = [
        pexpect.TIMEOUT,
        pexpect.EOF,
        PROMPTS["email"],
        PROMPTS["password"],
        PROMPTS["totp"],
        PROMPTS["mailbox_password"],
        PROMPTS["done"],
        PROMPTS["error"],
    ]

    totp_sent = False
    mailbox_sent = False

    while True:
        idx = child.expect(patterns, timeout=PROMPT_TIMEOUT)

        if idx == 0:  # TIMEOUT
            raise pexpect.TIMEOUT("No prompt received within timeout.")

        if idx == 1:  # EOF — process exited
            return

        if idx == 2:  # email prompt
            log.debug("CLI requested email.")
            child.sendline(email)

        elif idx == 3:  # password prompt
            # The CLI may show a password prompt for both the account password
            # and the mailbox password.  We track which we've already sent.
            if not mailbox_sent and mailbox_password and totp_sent:
                log.debug("CLI requested mailbox password.")
                child.sendline(mailbox_password)
                mailbox_sent = True
            else:
                log.debug("CLI requested account password.")
                child.sendline(password)

        elif idx == 4:  # TOTP prompt
            log.debug("CLI requested TOTP code.")
            child.sendline(totp_code)
            totp_sent = True

        elif idx == 5:  # explicit mailbox password prompt
            log.debug("CLI requested mailbox password (explicit prompt).")
            child.sendline(mailbox_password or "")
            mailbox_sent = True

        elif idx == 6:  # export done
            log.info("CLI reported export complete.")
            return

        elif idx == 7:  # error line
            snippet = (child.before or "").strip().splitlines()[-1]
            raise ExportError(f"CLI reported an error: {snippet}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _PexpectLogger:
    """Thin adapter so pexpect writes its read data to our logger at DEBUG."""

    def __init__(self, logger):
        self._log = logger

    def write(self, s: str) -> None:
        if s.strip():
            self._log.debug("[cli] %s", s.rstrip())

    def flush(self) -> None:
        pass
