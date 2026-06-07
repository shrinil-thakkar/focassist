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
    from agent.categorizer import load_rules

    # Refresh rules from backend
    backend_rules = sync.get_rules()
    load_rules(backend_rules)

    # Fetch + categorize
    try:
        events = tracker.fetch_events(date.today())
    except Exception as e:
        log.error("Failed to fetch AW events: %s", e)
        events = {"window": [], "web": []}

    from agent.categorizer import categorize_events
    from agent.session_detector import detect_sessions, build_timeline
    aggregates, ambiguous = categorize_events(events)
    sessions = detect_sessions(events)
    timeline = build_timeline(events)

    # Push to backend
    sync.push_aggregates(date.today(), aggregates, ambiguous, sessions, timeline)

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
