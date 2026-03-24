"""Manage the bundled Proton Mail Export CLI binary.

The binary lives in  mailrelay/tools/proton-export/proton-mail-export-cli
and is downloaded on first use (with user consent).

Public API
----------
ensure_export_cli() -> Path
    Return the path to the binary, downloading it first if needed.
    Raises ToolSetupError if the user declines or the download fails.

BINARY_PATH : Path
    Absolute path to where the binary is expected.
"""

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from .logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

TOOLS_DIR = Path(__file__).parent.parent / "tools" / "proton-export"
BINARY_NAME = "proton-mail-export-cli"
BINARY_PATH = TOOLS_DIR / BINARY_NAME

DOWNLOAD_URL = (
    "https://proton.me/download/export-tool/proton-mail-export-cli-linux_x86_64.tar.gz"
)
ARCHIVE_NAME = "proton-mail-export-cli-linux_x86_64.tar.gz"


class ToolSetupError(Exception):
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_export_cli() -> Path:
    """Return the path to proton-mail-export-cli, downloading if necessary.

    Raises ToolSetupError if the binary is unavailable and the user declines
    to download it, or if the download / extraction fails.
    """
    if BINARY_PATH.exists():
        log.debug("Export CLI found at %s", BINARY_PATH)
        return BINARY_PATH

    log.info("Export CLI not found at %s", BINARY_PATH)
    _prompt_and_download()
    return BINARY_PATH


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _prompt_and_download() -> None:
    """Ask the user whether to download the CLI, then do it."""
    print(
        "\nThe Proton Mail Export CLI is required but was not found.\n"
        f"It will be downloaded from:\n  {DOWNLOAD_URL}\n"
        f"and installed to:\n  {BINARY_PATH}\n"
    )

    answer = input("Download now? [Y/n]: ").strip().lower()
    if answer and answer not in ("y", "yes"):
        raise ToolSetupError(
            "Download declined. Re-run and choose Y, or place the binary at:\n"
            f"  {BINARY_PATH}"
        )

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = TOOLS_DIR / ARCHIVE_NAME

    _download(DOWNLOAD_URL, archive_path)
    _extract(archive_path, TOOLS_DIR)

    if not BINARY_PATH.exists():
        raise ToolSetupError(
            f"Extraction completed but '{BINARY_NAME}' not found in {TOOLS_DIR}.\n"
            "The archive layout may have changed — check the contents manually."
        )

    # Ensure the binary is executable
    BINARY_PATH.chmod(BINARY_PATH.stat().st_mode | 0o755)
    log.info("Export CLI ready at %s", BINARY_PATH)

    # Remove the archive to keep the tools directory tidy
    try:
        archive_path.unlink()
    except OSError:
        pass


def _download(url: str, dest: Path) -> None:
    """Download *url* to *dest* using wget (with progress output)."""
    if not shutil.which("wget"):
        raise ToolSetupError(
            "'wget' is required to download the export tool but was not found on PATH."
        )

    log.info("Downloading %s …", url)
    result = subprocess.run(
        ["wget", "--show-progress", "-O", str(dest), url],
        check=False,
    )
    if result.returncode != 0:
        # Clean up partial download
        if dest.exists():
            dest.unlink()
        raise ToolSetupError(
            f"wget exited with code {result.returncode}. Check your network connection."
        )
    log.info("Download complete: %s", dest.name)


def _extract(archive_path: Path, dest_dir: Path) -> None:
    """Extract a .tar.gz archive into *dest_dir*."""
    log.info("Extracting %s …", archive_path.name)
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            # Safety: skip any members with absolute paths or path traversal
            safe_members = [
                m for m in tar.getmembers()
                if not os.path.isabs(m.name) and ".." not in m.name
            ]
            tar.extractall(path=dest_dir, members=safe_members)
    except tarfile.TarError as exc:
        raise ToolSetupError(f"Failed to extract archive: {exc}") from exc

    # If the binary landed inside a subdirectory, hoist it up
    if not BINARY_PATH.exists():
        for candidate in dest_dir.rglob(BINARY_NAME):
            shutil.move(str(candidate), str(BINARY_PATH))
            log.debug("Moved binary from %s to %s", candidate, BINARY_PATH)
            break
