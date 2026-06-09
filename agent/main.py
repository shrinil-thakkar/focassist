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


def run_cycle() -> None:
    from agent import tracker, sync, blocker
    from agent.categorizer import load_rules, categorize_events
    from agent.timeline import resolve_timeline, detect_flags
    from agent.session_detector import (
        detect_sessions, build_timeline, build_hourly_aggregates, build_daily_aggregates,
    )
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")

    # Refresh rules from backend
    backend_rules = sync.get_rules()
    load_rules(backend_rules)

    # Fetch events
    try:
        events = tracker.fetch_events(date.today())
    except Exception as e:
        log.error("Failed to fetch AW events: %s", e)
        events = {"window": [], "web": []}

    # Resolve once — all derived quantities share this single AFK-anchored pass
    # (tracking-algorithm.md §9).
    resolved = resolve_timeline(events)

    # Fix B: derive daily aggregates from the resolved timeline so each browser
    # interval is represented exactly once (no window/web duplication).
    aggregates = build_daily_aggregates(events, resolved=resolved)
    _, ambiguous = categorize_events(events)  # keep for ambiguous-queue suggestions

    sessions = detect_sessions(events, resolved=resolved)
    timeline = build_timeline(events, resolved=resolved)
    hourly = build_hourly_aggregates(events, resolved=resolved)

    # First non-untracked interval → "tracked from X" in the daily report header.
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

    # Push to backend
    sync.push_aggregates(date.today(), aggregates, ambiguous, sessions, timeline, hourly, coverage)

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
