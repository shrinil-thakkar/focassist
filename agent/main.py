"""
Mac agent main loop.
Runs as a launchd login service (see com.focus.agent.plist).
Cycle every 5 minutes:
  1. Fetch today's AW events.
  2. Categorize (Tier-1 rules).
  3. Push aggregates + ambiguous queue to backend.
  4. Poll directive; start/clear website block as needed.
"""
import logging
import sys
import time
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("focassist.agent")

POLL_INTERVAL_SECONDS = 300  # 5 minutes


def _run_pipeline(target_date: date) -> None:
    """Fetch AW events for target_date, run the full classify pipeline, push to backend."""
    from agent import tracker, sync
    from agent.categorizer import categorize_events
    from agent.timeline import resolve_timeline, detect_flags
    from agent.session_detector import (
        detect_sessions, build_timeline, build_hourly_aggregates, build_daily_aggregates,
    )
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")

    try:
        events = tracker.fetch_events(target_date)
    except Exception as e:
        log.error("Failed to fetch AW events for %s: %s", target_date, e)
        return

    resolved = resolve_timeline(events)
    aggregates = build_daily_aggregates(events, resolved=resolved)
    _, ambiguous = categorize_events(events)
    sessions = detect_sessions(events, resolved=resolved)
    timeline = build_timeline(events, resolved=resolved)
    hourly = build_hourly_aggregates(events, resolved=resolved)

    first_tracked_ist = None
    for iv in resolved["timeline"]:
        if iv["state"] != "untracked":
            dt = iv["start"].astimezone(IST)
            h12 = dt.hour % 12 or 12
            period = "am" if dt.hour < 12 else "pm"
            first_tracked_ist = (
                f"{h12}:{dt.minute:02d}{period}" if dt.minute else f"{h12}{period}"
            )
            break

    coverage = {
        "active_minutes": resolved["active_minutes"],
        "idle_minutes": resolved["idle_minutes"],
        "untracked_minutes": resolved["untracked_minutes"],
        "flags": detect_flags(resolved),
        "first_tracked_ist": first_tracked_ist,
    }

    sync.push_aggregates(target_date, aggregates, ambiguous, sessions, timeline, hourly, coverage)


def run_cycle() -> None:
    from agent import sync, blocker
    from agent.categorizer import load_rules

    # Refresh rules from backend
    backend_rules = sync.get_rules()
    load_rules(backend_rules)

    # Normal daily pipeline
    _run_pipeline(date.today())

    # Reprocess any dates queued via /reprocess bot command
    for job_date_str in sync.get_reprocess_jobs():
        try:
            log.info("Reprocessing %s", job_date_str)
            _run_pipeline(date.fromisoformat(job_date_str))
            sync.mark_reprocess_done(job_date_str)
            log.info("Reprocess done for %s", job_date_str)
        except Exception as e:
            log.error("Reprocess failed for %s: %s", job_date_str, e)

    # Act on directive
    directive = sync.get_directive()
    if directive.get("focus_block_active") and directive.get("block_domains"):
        if not blocker.is_active():
            blocker.start_block(
                directive["block_domains"],
                directive["block_until"],
            )
    else:
        blocker.clear_expired_block()


def main() -> None:
    log.info("Focus assistant agent starting.")
    while True:
        try:
            run_cycle()
        except Exception as e:
            log.exception("Unexpected error in agent cycle: %s", e)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
