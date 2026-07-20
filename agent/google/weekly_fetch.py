"""Orchestrates the weekly Gmail + Calendar fetch, shared by fetch_week.py
(manual CLI run) and agent/main.py (automatic run queued via /fetch on Telegram).
"""

import json
import sys

from agent.google.calendar_client import get_past_events
from agent.google.gmail_client import fetch_emails


def fetch_and_write(
    days: int = 7,
    max_emails: int = 200,
    max_events: int = 20,
    emails_out: str = "emails_last_week.json",
    calendar_out: str = "calendar_last_week.json",
) -> tuple[int, int]:
    """Fetch and write both JSON files. Returns (email_count, event_count)."""
    print(f"Fetching last {days} day(s) of email...", file=sys.stderr)
    emails = fetch_emails(days=days, max_results=max_emails)
    with open(emails_out, "w") as f:
        json.dump(emails, f, indent=2)
    print(f"Wrote {len(emails)} emails to {emails_out}", file=sys.stderr)

    print(f"Fetching last {days} day(s) of calendar events...", file=sys.stderr)
    events = get_past_events(days=days, max_results=max_events)
    with open(calendar_out, "w") as f:
        json.dump(events, f, indent=2)
    print(f"Wrote {len(events)} events to {calendar_out}", file=sys.stderr)

    return len(emails), len(events)
