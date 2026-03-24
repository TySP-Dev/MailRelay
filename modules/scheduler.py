"""APScheduler wrapper for MailRelay's polling interval.

The scheduler runs a single persistent background job that fires the sync
function at the user-configured interval.  It also exposes helpers so main.py
can trigger an immediate run or print the next scheduled time.
"""

from datetime import datetime, timezone
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .logger import get_logger

log = get_logger(__name__)

JOB_ID = "mailrelay_sync"

_scheduler: Optional[BackgroundScheduler] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start(sync_fn: Callable, interval_minutes: int) -> None:
    """Initialise and start the scheduler with *interval_minutes* between runs.

    *sync_fn* is called with no arguments each time the interval fires.
    """
    global _scheduler

    if _scheduler and _scheduler.running:
        log.warning("Scheduler already running — ignoring start() call.")
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _guarded(sync_fn),
        trigger=IntervalTrigger(minutes=interval_minutes),
        id=JOB_ID,
        name="MailRelay sync",
        replace_existing=True,
        max_instances=1,      # prevent overlapping runs
        coalesce=True,        # skip missed fires rather than catching up
    )
    _scheduler.start()
    log.info(
        "Scheduler started. Sync will run every %d minute(s).", interval_minutes
    )


def stop() -> None:
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scheduler stopped.")
    _scheduler = None


def run_now(sync_fn: Callable) -> None:
    """Trigger an immediate sync outside the normal schedule."""
    log.info("Manual run triggered.")
    _guarded(sync_fn)()


def next_run_time() -> Optional[datetime]:
    """Return the next scheduled run time (UTC), or None if not scheduled."""
    if not _scheduler or not _scheduler.running:
        return None
    job = _scheduler.get_job(JOB_ID)
    if job and job.next_run_time:
        return job.next_run_time
    return None


def update_interval(interval_minutes: int) -> None:
    """Change the polling interval without restarting the scheduler."""
    if not _scheduler or not _scheduler.running:
        raise RuntimeError("Scheduler is not running.")
    _scheduler.reschedule_job(
        JOB_ID,
        trigger=IntervalTrigger(minutes=interval_minutes),
    )
    log.info("Polling interval updated to %d minute(s).", interval_minutes)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _guarded(fn: Callable) -> Callable:
    """Wrap *fn* so unhandled exceptions are logged but don't kill the scheduler."""
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            log.error("Unhandled exception in sync function: %s", exc, exc_info=True)
    return wrapper
