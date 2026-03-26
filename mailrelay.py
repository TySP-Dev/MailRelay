"""MailRelay — entry point and CLI.

Usage examples:
  python mailrelay.py --setup          First-time setup wizard (installs deps + configures)
  python mailrelay.py                  Start the background service
  python mailrelay.py --debug          Start with debug logs
  python mailrelay.py --run-now        Immediate sync then keep running
  python mailrelay.py --status         Print last-run summary and exit
  python mailrelay.py --logs           Tail the log file and exit
  python mailrelay.py --config         Interactively change a single config value
"""

import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Python version check — must happen before any other imports
# ---------------------------------------------------------------------------

if sys.version_info < (3, 11):
    print(
        f"ERROR: Python 3.11+ is required "
        f"(found {sys.version_info.major}.{sys.version_info.minor})."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Virtual environment bootstrap — runs only during --setup, outside a venv
# ---------------------------------------------------------------------------

def _bootstrap_venv() -> None:
    """If --setup was requested and we're not in a venv, create one and re-exec.

    Creates .venv/, installs requirements.txt, then replaces the current
    process with the venv Python carrying the same arguments.  After re-exec
    this function returns immediately because sys.prefix != sys.base_prefix.
    """
    if "--setup" not in sys.argv:
        return
    if sys.prefix != sys.base_prefix:
        return  # already inside a virtual environment

    project_dir = Path(__file__).parent
    venv_dir = project_dir / ".venv"
    is_windows = sys.platform == "win32"
    bin_dir = venv_dir / ("Scripts" if is_windows else "bin")
    python_exe = bin_dir / ("python.exe" if is_windows else "python")
    pip_exe = bin_dir / ("pip.exe" if is_windows else "pip")

    if not venv_dir.exists():
        print("Creating virtual environment at .venv ...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        print("Virtual environment created.")
    else:
        print("Virtual environment already exists — skipping creation.")

    print("Installing dependencies from requirements.txt ...")
    subprocess.run(
        [str(pip_exe), "install", "--quiet", "--upgrade", "pip"],
        check=True,
    )
    subprocess.run(
        [str(pip_exe), "install", "--quiet", "-r", str(project_dir / "requirements.txt")],
        check=True,
    )
    print("Dependencies installed.\n")

    # Replace this process with the venv Python, forwarding all arguments
    os.execv(str(python_exe), [str(python_exe)] + sys.argv)


_bootstrap_venv()


# ---------------------------------------------------------------------------
# Imports — safe after bootstrap ensures deps are present
# ---------------------------------------------------------------------------

import getpass
import shutil
import signal
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import typer

from modules import config as cfg
from modules import database
from modules import exporter
from modules import forwarder
from modules import otp
from modules import packager
from modules import processor
from modules import scheduler
from modules import tools
from modules.logger import get_logger, configure_logging, tail_log

log = get_logger(__name__)
app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

# In-memory master password for the lifetime of the process
_master_password: Optional[str] = None

# Simple in-memory last-run summary (persisted to log; this is for --status)
_last_run: dict = {}


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@app.command()
def main(
    setup: bool = typer.Option(False, "--setup", help="Run first-time setup wizard."),
    run_now: bool = typer.Option(False, "--run-now", help="Trigger an immediate sync."),
    status: bool = typer.Option(False, "--status", help="Print status summary and exit."),
    logs: bool = typer.Option(False, "--logs", help="Tail the log file and exit."),
    change_config: bool = typer.Option(False, "--config", help="Change a config value."),
    debug: bool = typer.Option(False, "--debug", help="Show all debug output on the console."),
) -> None:
    global _master_password

    configure_logging(debug=debug)

    if setup:
        _run_setup()
        return

    if logs:
        print(tail_log(lines=100))
        return

    # All other commands need the config to be decrypted
    _master_password = _prompt_master_password()

    if status:
        _print_status()
        return

    if change_config:
        _run_config_change()
        return

    # Default: start the service
    _start_service(run_now=run_now)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def _ensure_data_dirs() -> None:
    """Create data/, data/exports/, and data/downloads/ if they don't exist."""
    base = Path(__file__).parent / "data"
    for sub in ("", "exports", "downloads"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def _run_setup() -> None:
    if cfg.config_exists():
        overwrite = typer.confirm(
            "A config already exists. Overwrite it?", default=False
        )
        if not overwrite:
            raise typer.Abort()

    _ensure_data_dirs()

    config_data, master_pw = cfg.build_config_interactively()

    # Only download the Proton CLI if Proton is involved as source or destination
    export_service = config_data["preferences"]["export_service"]
    forward_service = config_data["preferences"]["forward_service"]
    if export_service == "proton" or forward_service == "proton":
        try:
            tools.ensure_export_cli()
        except tools.ToolSetupError as exc:
            log.error("Export CLI setup failed: %s", exc)
            raise typer.Exit(code=1)

    cfg.save_config(config_data, master_pw)
    database.init_db()
    print("\nSetup complete. Run 'python mailrelay.py' to start MailRelay.")


# ---------------------------------------------------------------------------
# Service startup
# ---------------------------------------------------------------------------

def _start_service(run_now: bool = False) -> None:
    global _master_password

    _ensure_data_dirs()

    conf = _load_config()

    # Only need the Proton CLI if Proton is a source or destination
    export_service = conf["preferences"]["export_service"]
    forward_service = conf["preferences"]["forward_service"]
    if export_service == "proton" or forward_service == "proton":
        try:
            tools.ensure_export_cli()
        except tools.ToolSetupError as exc:
            log.error("Export CLI unavailable: %s", exc)
            raise typer.Exit(code=1)

    database.init_db()
    packager.start_server()

    interval = conf["preferences"]["poll_interval_min"]
    _sync_lock = threading.Lock()

    def sync():
        if _sync_lock.acquire(blocking=False):
            try:
                _sync_cycle(conf)
            finally:
                _sync_lock.release()
        else:
            log.info("Sync already in progress — skipping.")

    scheduler.start(sync, interval)

    # Auto-run if this is the first sync ever, or the last one was overdue
    if run_now:
        scheduler.run_now(sync)
    else:
        last = database.get_last_sync_time()
        if last is None:
            log.info("No previous sync found — running immediately.")
            scheduler.run_now(sync)
        else:
            elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if elapsed_min >= interval:
                log.info(
                    "Last sync was %.0f minute(s) ago (interval: %d min) — running immediately.",
                    elapsed_min, interval,
                )
                scheduler.run_now(sync)

    _start_stdin_listener(sync)
    log.info("MailRelay running. Type 'now' + Enter to sync immediately. Press Ctrl+C to stop.")

    # Keep the main thread alive; handle Ctrl+C / SIGTERM gracefully
    def _shutdown(sig, frame):
        log.info("Shutdown signal received.")
        scheduler.stop()
        packager.stop_server()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            time.sleep(1)
    except SystemExit:
        pass


# ---------------------------------------------------------------------------
# Core sync cycle
# ---------------------------------------------------------------------------

def _sync_cycle(conf: dict) -> None:
    global _last_run
    start_time = datetime.now(timezone.utc)
    log.info("=== Sync cycle started at %s ===", start_time.isoformat())

    summary = {
        "started_at": start_time.isoformat(),
        "emails_found": 0,
        "emails_new": 0,
        "delivered": 0,
        "failed": 0,
        "fallback": False,
        "download_url": None,
        "error": None,
    }

    try:
        # 1. Clean up any stale MBOX files from the previous cycle
        stale = packager.cleanup_stale()
        if stale:
            log.info("%d stale pending ID(s) cleared for re-processing.", len(stale))

        # 2. Fetch/export from source
        export_service = conf["preferences"]["export_service"]
        if export_service == "proton":
            totp_code = otp.generate_totp(conf["proton"]["totp_secret"])
            export_dir = exporter.run_export(
                email=conf["proton"]["email"],
                password=conf["proton"]["password"],
                totp_code=totp_code,
                mailbox_password=conf["proton"].get("mailbox_password", ""),
            )
        else:
            export_dir = exporter.run_imap_fetch(
                service=export_service,
                email_addr=conf[export_service]["email"],
                password=conf[export_service]["password"],
            )

        # 3. Scan, pair, deduplicate
        new_emails = processor.scan_and_filter(export_dir)
        summary["emails_found"] = len(list(export_dir.glob("*.eml")))
        summary["emails_new"] = len(new_emails)

        if not new_emails:
            log.info("No new emails to deliver.")
        else:
            delivery_mode = conf["preferences"]["delivery_mode"]
            _deliver(conf, new_emails, delivery_mode, conf["preferences"]["forward_service"], summary)

        # 4. Clean up export directory
        _clean_export_dir(export_dir)

    except exporter.ExportError as exc:
        log.error("Export failed: %s", exc)
        summary["error"] = str(exc)
    except Exception as exc:
        log.error("Unexpected error in sync cycle: %s", exc, exc_info=True)
        summary["error"] = str(exc)
    else:
        database.record_sync_time()
    finally:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        _last_run = summary
        log.info(
            "=== Sync complete — new: %d, delivered: %d, failed: %d%s ===",
            summary["emails_new"],
            summary["delivered"],
            summary["failed"],
            " [FALLBACK to MBOX]" if summary["fallback"] else "",
        )


def _deliver(
    conf: dict,
    emails: list,
    mode: str,
    forward_service: str,
    summary: dict,
) -> None:
    if mode == "proton":
        _deliver_proton(conf, emails, summary)
    elif mode == "imap":
        section = f"{forward_service}_receive"
        imap_host, imap_port = forwarder.IMAP_SETTINGS[forward_service]
        succeeded, failed = forwarder.push_emails(
            emails,
            dest_email=conf[section]["email"],
            dest_password=conf[section]["password"],
            imap_host=imap_host,
            imap_port=imap_port,
        )
        summary["delivered"] = len(succeeded)
        summary["failed"] = len(failed)

        if failed:
            log.warning(
                "%d message(s) failed IMAP push — falling back to MBOX for this cycle.",
                len(failed),
            )
            summary["fallback"] = True
            failed_emails = [e for e in emails if e.message_id in failed]
            _deliver_mbox(failed_emails, summary)
    else:
        _deliver_mbox(emails, summary)


def _deliver_proton(conf: dict, emails: list, summary: dict) -> None:
    """Restore new emails into the destination Proton account via the CLI."""
    receive_conf = conf["proton_receive"]

    # Build a matching export directory structure in a temp location
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    restore_base = Path(tempfile.mkdtemp())
    restore_mail_dir = restore_base / receive_conf["email"] / f"mail_{timestamp}"
    restore_mail_dir.mkdir(parents=True)

    try:
        for rich in emails:
            (restore_mail_dir / f"{rich.message_id}.eml").write_bytes(rich.raw_bytes)

        totp_code = otp.generate_totp(receive_conf["totp_secret"])
        exporter.run_restore(
            email=receive_conf["email"],
            password=receive_conf["password"],
            totp_code=totp_code,
            restore_base=restore_base,
            mailbox_password=receive_conf.get("mailbox_password", ""),
        )
        database.mark_delivered([e.message_id for e in emails])
        summary["delivered"] = len(emails)
        log.info("Proton restore delivered %d message(s).", len(emails))
    except exporter.ExportError as exc:
        log.error("Proton restore failed: %s", exc)
        summary["failed"] = len(emails)
    finally:
        shutil.rmtree(restore_base, ignore_errors=True)


def _deliver_mbox(emails: list, summary: dict) -> None:
    url = packager.bundle_emails(emails)
    if url:
        summary["download_url"] = url
        summary["delivered"] += len(emails)
        log.info("MBOX download available at: %s", url)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _print_status() -> None:
    conf = _load_config()
    mode = conf["preferences"]["delivery_mode"]
    interval = conf["preferences"]["poll_interval_min"]
    next_run = scheduler.next_run_time()
    pending = database.get_pending_mboxes()

    print("\n=== MailRelay Status ===\n")
    print(f"  Delivery mode   : {mode.upper()}")
    print(f"  Poll interval   : {interval} minutes")

    if next_run:
        print(f"  Next sync       : {next_run.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    else:
        print("  Next sync       : (scheduler not running)")

    if _last_run:
        print(f"\n  Last run        : {_last_run.get('started_at', 'n/a')}")
        print(f"  New emails      : {_last_run.get('emails_new', 0)}")
        print(f"  Delivered       : {_last_run.get('delivered', 0)}")
        print(f"  Failed          : {_last_run.get('failed', 0)}")
        if _last_run.get("fallback"):
            print("  Fallback used   : YES (IMAP failed → MBOX)")
        if _last_run.get("error"):
            print(f"  Last error      : {_last_run['error']}")
    else:
        print("\n  Last run        : (no run yet this session)")

    if pending:
        print(f"\n  Pending MBOX downloads ({len(pending)}):")
        for entry in pending:
            path = Path(entry["mbox_path"])
            count = len(entry["message_ids"])
            url = f"http://{packager.SERVER_HOST}:{packager.SERVER_PORT}/download/{path.name}"
            print(f"    {path.name}  ({count} message(s))  →  {url}")
    else:
        print("\n  Pending downloads: none")

    print()


# ---------------------------------------------------------------------------
# Config change
# ---------------------------------------------------------------------------

def _run_config_change() -> None:
    print("\nAvailable settings:")
    print("  proton.email | proton.password | proton.mailbox_password | proton.totp_secret")
    print("  proton_receive.email | proton_receive.password | proton_receive.mailbox_password | proton_receive.totp_secret")
    print("  gmail.email | gmail.password | gmail_receive.email | gmail_receive.password")
    print("  outlook.email | outlook.password | outlook_receive.email | outlook_receive.password")
    print("  icloud.email | icloud.password | icloud_receive.email | icloud_receive.password")
    print("  preferences.delivery_mode | preferences.poll_interval_min")
    print()

    key_path = input("Setting to change (e.g. preferences.delivery_mode): ").strip()
    if "." not in key_path:
        print("Please use section.key format.")
        return

    section, key = key_path.split(".", 1)
    new_value_raw = getpass.getpass(f"New value for {key_path}: ") \
        if "password" in key.lower() or "secret" in key.lower() \
        else input(f"New value for {key_path}: ").strip()

    # Coerce poll_interval_min to int
    if key == "poll_interval_min":
        try:
            new_value = int(new_value_raw)
        except ValueError:
            print("poll_interval_min must be an integer.")
            return
    else:
        new_value = new_value_raw

    cfg.update_config(_master_password, section, key, new_value)
    print(f"\nUpdated {key_path} successfully.")

    if key == "poll_interval_min" and scheduler._scheduler and scheduler._scheduler.running:
        scheduler.update_interval(int(new_value))
        print(f"Live polling interval updated to {new_value} minutes.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_stdin_listener(sync_fn) -> None:
    """Start a daemon thread that watches stdin for the 'now' keyword.

    Typing 'now' + Enter triggers an immediate sync without touching the
    scheduler's interval — the next scheduled run fires at its normal time.
    A lock inside sync_fn prevents two syncs from running simultaneously.
    """
    def _reader():
        while True:
            try:
                line = sys.stdin.readline()
            except (EOFError, OSError):
                break
            if line.strip().lower() == "now":
                log.info("'now' received — triggering immediate sync.")
                threading.Thread(
                    target=sync_fn,
                    daemon=True,
                    name="mailrelay-adhoc-sync",
                ).start()

    threading.Thread(target=_reader, daemon=True, name="mailrelay-stdin-listener").start()


def _prompt_master_password() -> str:
    # Allow headless/server use via environment variable
    env_pw = os.environ.get("MAILRELAY_MASTER_PASSWORD")
    if env_pw:
        return env_pw
    return getpass.getpass("Master password: ")


def _load_config() -> dict:
    try:
        return cfg.load_config(_master_password)
    except cfg.ConfigError as exc:
        log.error("%s", exc)
        raise typer.Exit(code=1)


def _clean_export_dir(export_dir: Path) -> None:
    """Remove the mail_* export directory tree after a sync cycle."""
    try:
        shutil.rmtree(export_dir)
        log.debug("Removed export directory: %s", export_dir.name)
    except OSError as exc:
        log.warning("Could not remove export directory %s: %s", export_dir.name, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
