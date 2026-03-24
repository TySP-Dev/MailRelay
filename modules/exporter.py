"""Automate proton-mail-export-cli via pexpect.

The Proton Export CLI interactive prompt sequence (v1.0.6):
  1. Username
  2. Password
  3. Two-factor authentication code   (only if 2FA is enabled)
  4. Mailbox password                  (only if separate mailbox password is set)
  5. Operation (B)ackup/(R)estore      — we always send 'B'
  6. "Do you wish to proceed?"         — we always send 'Yes'

The CLI creates the following directory structure under the base export dir:
  <base>/<Email>/mail_<YYYYMMDD_HHMMSS>/
    <messageID>.eml
    <messageID>.metadata.json
    ...

run_export() returns the innermost mail_* directory so callers can
scan .eml files directly.
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
    "username":         r"[Uu]sername\s*:",
    "password":         r"[Pp]assword\s*:",
    "totp":             r"[Ee]nter the code from your authenticator",
    "mailbox_password": r"[Mm]ailbox [Pp]assword\s*:",
    "operation":        r"[Oo]peration\s*\(\(B\)ackup",
    "proceed":          r"[Dd]o you wish to proceed\?",
    "starting":         r"Starting Export",
    "error":            r"[Ee]rror|[Ff]ailed|[Ii]nvalid|[Uu]nexpected",
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
    """Drive proton-mail-export-cli and return the mail_* export directory.

    The CLI creates <base>/<Email>/mail_<timestamp>/ — this function finds
    and returns that innermost directory.

    Raises ExportError on authentication failure or unexpected CLI output.
    """
    base_dir = export_dir or EXPORT_DIR
    base_dir.mkdir(parents=True, exist_ok=True)

    log.info("Starting Proton Mail export...")

    try:
        binary = ensure_export_cli()
    except Exception as exc:
        raise ExportError(str(exc)) from exc

    cmd = [str(binary), "--dir", str(base_dir)]
    log.debug("Running: %s", " ".join(cmd))

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
        log.debug("CLI process finished (EOF).")

    mail_dir = _find_mail_dir(base_dir, email)
    log.info("Proton Mail export complete. Files in: %s", mail_dir)
    return mail_dir


# ---------------------------------------------------------------------------
# Interactive prompt driver
# ---------------------------------------------------------------------------

def _drive_cli(
    child: pexpect.spawn,
    email: str,
    password: str,
    totp_code: str,
    mailbox_password: str,
) -> None:
    """Respond to each interactive prompt in sequence."""
    patterns = [
        pexpect.TIMEOUT,                   # 0
        pexpect.EOF,                       # 1
        PROMPTS["username"],               # 2
        PROMPTS["password"],               # 3
        PROMPTS["totp"],                   # 4
        PROMPTS["mailbox_password"],       # 5
        PROMPTS["operation"],              # 6
        PROMPTS["proceed"],                # 7
        PROMPTS["starting"],               # 8
        PROMPTS["error"],                  # 9
    ]

    totp_sent = False
    mailbox_sent = False

    while True:
        idx = child.expect(patterns, timeout=PROMPT_TIMEOUT)

        if idx == 0:  # TIMEOUT
            raise pexpect.TIMEOUT("No prompt received within timeout.")

        if idx == 1:  # EOF — process exited
            return

        if idx == 2:  # Username
            log.debug("CLI requested username.")
            child.sendline(email)

        elif idx == 3:  # Password (account or mailbox)
            if not mailbox_sent and mailbox_password and totp_sent:
                log.debug("CLI requested mailbox password.")
                child.sendline(mailbox_password)
                mailbox_sent = True
            else:
                log.debug("CLI requested account password.")
                child.sendline(password)

        elif idx == 4:  # TOTP
            log.debug("CLI requested TOTP code.")
            child.sendline(totp_code)
            totp_sent = True

        elif idx == 5:  # Explicit mailbox password prompt
            log.debug("CLI requested mailbox password (explicit prompt).")
            child.sendline(mailbox_password or "")
            mailbox_sent = True

        elif idx == 6:  # Operation prompt
            log.debug("CLI requested operation — sending 'B' for backup.")
            child.sendline("B")

        elif idx == 7:  # "Do you wish to proceed?"
            log.debug("CLI asked to confirm export path — sending 'Yes'.")
            child.sendline("Yes")

        elif idx == 8:  # "Starting Export" — export is underway, wait for EOF
            log.debug("CLI reported export started — waiting for completion.")
            child.expect(pexpect.EOF, timeout=EXPORT_TIMEOUT)
            return

        elif idx == 9:  # Error line
            snippet = (child.before or "").strip().splitlines()[-1]
            raise ExportError(f"CLI reported an error: {snippet}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_mail_dir(base_dir: Path, email: str) -> Path:
    """Locate the mail_* directory the CLI created under base_dir.

    The CLI creates:  base_dir/<Email>/mail_<YYYYMMDD_HHMMSS>/
    The email folder name may differ in capitalisation from the supplied
    address, so we match case-insensitively.
    """
    # Find the email-named subdirectory
    email_dirs = [
        d for d in base_dir.iterdir()
        if d.is_dir() and d.name.lower() == email.lower()
    ]
    if not email_dirs:
        # Fallback: take any subdirectory (there should be exactly one)
        email_dirs = [d for d in base_dir.iterdir() if d.is_dir()]

    if not email_dirs:
        raise ExportError(
            f"Export finished but no output directory found under {base_dir}."
        )

    email_dir = email_dirs[0]

    mail_dirs = sorted(
        [d for d in email_dir.iterdir() if d.is_dir() and d.name.startswith("mail_")],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if not mail_dirs:
        raise ExportError(
            f"Export finished but no mail_* directory found under {email_dir}."
        )

    return mail_dirs[0]


class _PexpectLogger:
    """Thin adapter so pexpect writes its read data to our logger at DEBUG."""

    def __init__(self, logger):
        self._log = logger

    def write(self, s: str) -> None:
        if s.strip():
            self._log.debug("[cli] %s", s.rstrip())

    def flush(self) -> None:
        pass
