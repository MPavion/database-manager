#!/usr/bin/env python3
"""
Notion Maintenance  v1.0
------------------------
Runs scheduled Notion housekeeping jobs (deduplication, archiving, etc.)
without touching Claude — all defined in maintenance_config.json.

Usage
─────
  python notion_maintenance.py              # Run all due jobs once, then exit
  python notion_maintenance.py --daemon     # Run continuously on schedule
  python notion_maintenance.py --job NAME   # Run one specific job by name
  python notion_maintenance.py --list       # List all configured jobs
  python notion_maintenance.py --status     # Show last-run times for all jobs

All jobs are defined in maintenance_config.json (same folder as this script).
Each job points to a standard Notion Controller instructions JSON file.

Schedule format
───────────────
  "schedule": "daily"          runs every day at the specified time
  "schedule": "weekly"         runs on the specified weekday
  "schedule": "hourly"         runs every N hours
  "schedule": "on_demand"      never runs automatically; use --job NAME
  "schedule": "startup"        runs once each time --daemon is started

Requires: notion_controller.py in the same directory.
"""

import json
import os
import sys
import time
import logging
import argparse
import importlib.util
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Locate and import notion_controller from the same directory
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent.resolve()

def _import_controller():
    spec = importlib.util.spec_from_file_location(
        "notion_controller", _HERE / "notion_controller.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_nc = None
NotionClient = Any
OperationExecutor = None
load_config = None


def _ensure_controller_loaded():
    global _nc, NotionClient, OperationExecutor, load_config

    if _nc is not None and OperationExecutor is not None and load_config is not None:
        return

    try:
        _nc = _import_controller()
        NotionClient = _nc.NotionClient
        OperationExecutor = _nc.OperationExecutor
        load_config = _nc.load_config
    except Exception as exc:
        raise RuntimeError(
            f"Could not load notion_controller.py — {exc}. "
            "Make sure notion_controller.py is in the same folder as this script."
        ) from exc

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_PATH  = _HERE / "maintenance_config.json"
STATE_PATH   = _HERE / ".maintenance_state.json"   # tracks last-run times
LOG_PATH     = _HERE / "notion_maintenance.log"

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("notion_maintenance")

# ---------------------------------------------------------------------------
# Config & state helpers
# ---------------------------------------------------------------------------

def load_maintenance_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error(f"Config not found: {CONFIG_PATH}")
        log.error("Copy maintenance_config.json to the same folder as this script.")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Schedule evaluation
# ---------------------------------------------------------------------------

WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _is_due(job: dict, state: dict) -> bool:
    """
    Return True if this job should run now based on its schedule and
    the last time it ran (stored in state).
    """
    name     = job["name"]
    schedule = job.get("schedule", "on_demand").lower()
    last_run = state.get(name)  # ISO string or None

    if schedule == "on_demand":
        return False

    if schedule == "startup":
        # Run once per daemon session — check if run today
        if not last_run:
            return True
        return datetime.fromisoformat(last_run).date() < datetime.now().date()

    last_dt = datetime.fromisoformat(last_run) if last_run else None

    if schedule == "hourly":
        interval_hours = job.get("every_hours", 1)
        if not last_dt:
            return True
        return datetime.now() >= last_dt + timedelta(hours=interval_hours)

    if schedule == "daily":
        run_time = job.get("time", "02:00")
        h, m     = map(int, run_time.split(":"))
        now      = datetime.now()
        scheduled_today = now.replace(hour=h, minute=m, second=0, microsecond=0)

        if now < scheduled_today:
            return False  # not reached today's run time yet
        if not last_dt:
            return True
        # Has it run since today's scheduled time?
        return last_dt < scheduled_today

    if schedule == "weekly":
        run_time  = job.get("time", "02:00")
        run_day   = WEEKDAY_MAP.get(job.get("day", "monday").lower(), 0)
        h, m      = map(int, run_time.split(":"))
        now       = datetime.now()

        # Find the most recent occurrence of run_day+time
        days_ago  = (now.weekday() - run_day) % 7
        last_sched = now.replace(
            hour=h, minute=m, second=0, microsecond=0
        ) - timedelta(days=days_ago)
        if last_sched > now:
            last_sched -= timedelta(weeks=1)

        if not last_dt:
            return True
        return last_dt < last_sched

    return False


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def run_job(job: dict, client: NotionClient, dry_run_override: bool = False) -> bool:
    """
    Execute a single maintenance job.
    Returns True on success, False on failure.
    """
    _ensure_controller_loaded()
    name           = job["name"]
    instructions_file = Path(job["instructions_file"])

    # Resolve relative paths from the config file's directory
    if not instructions_file.is_absolute():
        instructions_file = _HERE / instructions_file

    if not instructions_file.exists():
        log.error(f"[{name}] Instructions file not found: {instructions_file}")
        return False

    try:
        with open(instructions_file, encoding="utf-8") as f:
            instructions = json.load(f)
    except Exception as exc:
        log.error(f"[{name}] Cannot read instructions file: {exc}")
        return False

    # Apply dry_run override to all find_and_archive_dupes operations
    if dry_run_override:
        for op in instructions.get("operations", []):
            if op.get("type") == "find_and_archive_dupes":
                op["dry_run"] = True

    op_count = len(instructions.get("operations", []))
    log.info(f"[{name}] Starting — {op_count} operation(s)")

    executor = OperationExecutor(client)

    def job_log(msg: str):
        log.info(f"[{name}] {msg}")

    results = executor.run_batch(instructions, log_fn=job_log)

    success = sum(1 for r in results if r["status"] == "success")
    errors  = sum(1 for r in results if r["status"] == "error")

    if errors == 0:
        log.info(f"[{name}] ✓ Complete — {success}/{op_count} succeeded")
    else:
        log.warning(f"[{name}] ⚠ Complete — {success} succeeded, {errors} failed")

    # Save per-job results log if configured
    if job.get("save_results", False):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        rpath = instructions_file.parent / f"{instructions_file.stem}_results_{ts}.json"
        try:
            with open(rpath, "w") as f:
                json.dump(results, f, indent=2, default=str)
            log.info(f"[{name}] Results saved → {rpath}")
        except Exception as exc:
            log.warning(f"[{name}] Could not save results: {exc}")

    return errors == 0


# ---------------------------------------------------------------------------
# Main modes
# ---------------------------------------------------------------------------

def _get_client() -> NotionClient:
    _ensure_controller_loaded()
    config  = load_config()
    api_key = config.get("api_key") or os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        log.error(
            "No Notion API key found.\n"
            "  • Run notion_controller.py (GUI) once to save your key, OR\n"
            "  • Set the NOTION_API_KEY environment variable."
        )
        sys.exit(1)
    return NotionClient(api_key)


def cmd_list(config: dict):
    """Print all configured jobs."""
    jobs = config.get("jobs", [])
    state = load_state()
    print(f"\n{'NAME':<30} {'SCHEDULE':<14} {'LAST RUN':<22} INSTRUCTIONS FILE")
    print("─" * 100)
    for job in jobs:
        name      = job.get("name", "?")
        schedule  = job.get("schedule", "on_demand")
        sched_str = schedule
        if schedule == "daily":
            sched_str = f"daily @ {job.get('time', '02:00')}"
        elif schedule == "weekly":
            sched_str = f"{job.get('day','mon')} @ {job.get('time','02:00')}"
        elif schedule == "hourly":
            sched_str = f"every {job.get('every_hours',1)}h"
        last = state.get(name, "never")[:19] if state.get(name) else "never"
        print(f"{name:<30} {sched_str:<14} {last:<22} {job.get('instructions_file','?')}")
    print()


def cmd_status(config: dict):
    """Print last-run times."""
    state = load_state()
    jobs  = config.get("jobs", [])
    print(f"\n{'NAME':<30} {'LAST RUN':<22} STATUS")
    print("─" * 70)
    for job in jobs:
        name = job["name"]
        last = state.get(name)
        print(f"{name:<30} {(last[:19] if last else 'never'):<22} "
              f"{'✓ ran' if last else '—'}")
    print()


def cmd_run_once(config: dict, dry_run: bool = False):
    """Run ALL due jobs once, then exit."""
    client = _get_client()
    state  = load_state()
    jobs   = config.get("jobs", [])

    ran = 0
    for job in jobs:
        if not _is_due(job, state):
            log.debug(f"Skipping '{job['name']}' — not due yet")
            continue
        success = run_job(job, client, dry_run_override=dry_run)
        state[job["name"]] = _now_str()
        save_state(state)
        ran += 1

    if ran == 0:
        log.info("No jobs were due. Use --job NAME to run one manually.")
    else:
        log.info(f"Ran {ran} job(s).")


def cmd_run_job(config: dict, job_name: str, dry_run: bool = False):
    """Run a single named job immediately regardless of schedule."""
    client = _get_client()
    jobs   = config.get("jobs", [])
    job    = next((j for j in jobs if j["name"].lower() == job_name.lower()), None)

    if not job:
        names = [j["name"] for j in jobs]
        log.error(f"Job '{job_name}' not found. Available: {names}")
        sys.exit(1)

    success = run_job(job, client, dry_run_override=dry_run)
    state   = load_state()
    state[job["name"]] = _now_str()
    save_state(state)
    sys.exit(0 if success else 1)


def cmd_daemon(config: dict, dry_run: bool = False):
    """
    Run continuously, checking every minute whether any jobs are due.
    Handles startup jobs on first tick.
    """
    client  = _get_client()
    state   = load_state()
    jobs    = config.get("jobs", [])
    poll    = config.get("poll_interval_seconds", 60)

    log.info(f"Daemon started — polling every {poll}s.  Press Ctrl+C to stop.")
    log.info(f"Log file: {LOG_PATH}")

    # Run startup jobs immediately
    for job in jobs:
        if job.get("schedule", "").lower() == "startup":
            log.info(f"Startup job: {job['name']}")
            run_job(job, client, dry_run_override=dry_run)
            state[job["name"]] = _now_str()
    save_state(state)

    try:
        while True:
            time.sleep(poll)
            state = load_state()
            for job in jobs:
                if job.get("schedule", "").lower() in ("on_demand", "startup"):
                    continue
                if _is_due(job, state):
                    log.info(f"Triggering scheduled job: {job['name']}")
                    run_job(job, client, dry_run_override=dry_run)
                    state[job["name"]] = _now_str()
                    save_state(state)
    except KeyboardInterrupt:
        log.info("Daemon stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Notion Maintenance — scheduled housekeeping for Notion",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--daemon",  action="store_true",
        help="Run continuously on schedule (use with nohup or launchd/cron)"
    )
    parser.add_argument(
        "--job",  metavar="NAME",
        help="Run one specific job by name immediately"
    )
    parser.add_argument(
        "--list",  action="store_true",
        help="List all configured jobs"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show last-run times for all jobs"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Force dry_run=true on all find_and_archive_dupes operations"
    )
    args = parser.parse_args()

    config = load_maintenance_config()

    if args.list:
        cmd_list(config)
    elif args.status:
        cmd_status(config)
    elif args.job:
        cmd_run_job(config, args.job, dry_run=args.dry_run)
    elif args.daemon:
        cmd_daemon(config, dry_run=args.dry_run)
    else:
        # Default: run all due jobs once
        cmd_run_once(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
