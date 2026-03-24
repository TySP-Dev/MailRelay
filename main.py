"""MailRelay — entry point and CLI.

Usage examples:
  python main.py --setup          First-time setup wizard
  python main.py                  Start the background service (prompts for master password)
  python main.py --run-now        Immediate sync then keep running
  python main.py --status         Print last-run summary and exit
  python main.py --logs           Tail the log file and exit
  python main.py --config         Interactively change a single config value
"""

import getpass
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
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
from modules.logger import get_logger, tail_log

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
) -> None:
    global _master_password

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

def _run_setup() -> None:
    if cfg.config_exists():
        overwrite = typer.confirm(
            "A config already exists. Overwrite it?", default=False
        )
        if not overwrite:
            raise typer.Abort()

    # Ensure the export CLI is present before finishing setup
    try:
        tools.ensure_export_cli()
    except tools.ToolSetupError as exc:
        log.error("Export CLI setup failed: %s", exc)
        raise typer.Exit(code=1)

    config_data, master_pw = cfg.build_config_interactively()
    cfg.save_config(config_data, master_pw)
    database.init_db()
    print("\nSetup complete. Run 'python main.py' to start MailRelay.")


# ---------------------------------------------------------------------------
# Service startup
# ---------------------------------------------------------------------------

def _start_service(run_now: bool = False) -> None:
    global _master_password

    # Verify the export CLI is present (offers download if missing)
    try:
        tools.ensure_export_cli()
    except tools.ToolSetupError as exc:
        log.error("Export CLI unavailable: %s", exc)
        raise typer.Exit(code=1)

    conf = _load_config()
    database.init_db()
    packager.start_server()

    interval = conf["preferences"]["poll_interval_min"]

    def sync():
        _sync_cycle(conf)

    scheduler.start(sync, interval)

    if run_now:
        scheduler.run_now(sync)

    log.info("MailRelay running. Press Ctrl+C to stop.")

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

        # 2. Generate TOTP code
        totp_code = otp.generate_totp(conf["proton"]["totp_secret"])

        # 3. Run the export CLI
        export_dir = exporter.run_export(
            email=conf["proton"]["email"],
            password=conf["proton"]["password"],
            totp_code=totp_code,
            mailbox_password=conf["proton"].get("mailbox_password", ""),
        )

        # 4. Scan, pair, deduplicate
        new_emails = processor.scan_and_filter(export_dir)
        summary["emails_found"] = len(list(export_dir.glob("*.eml")))
        summary["emails_new"] = len(new_emails)

        if not new_emails:
            log.info("No new emails to deliver.")
        else:
            delivery_mode = conf["preferences"]["delivery_mode"]
            _deliver(conf, new_emails, delivery_mode, summary)

        # 5. Clean up export directory
        _clean_export_dir(export_dir)

    except exporter.ExportError as exc:
        log.error("Export failed: %s", exc)
        summary["error"] = str(exc)
    except Exception as exc:
        log.error("Unexpected error in sync cycle: %s", exc, exc_info=True)
        summary["error"] = str(exc)
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


def _deliver(conf: dict, emails: list, mode: str, summary: dict) -> None:
    if mode == "imap":
        succeeded, failed = forwarder.push_emails(
            emails,
            icloud_email=conf["icloud"]["email"],
            icloud_password=conf["icloud"]["password"],
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
    print("  icloud.email | icloud.password")
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
    """Delete all files in the export directory after a sync cycle."""
    for f in export_dir.iterdir():
        try:
            f.unlink()
        except OSError as exc:
            log.warning("Could not delete export file %s: %s", f.name, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
