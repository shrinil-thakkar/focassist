"""Fetch the last N days of Gmail + Google Calendar data as JSON.

Usage: python fetch_week.py [--days 7]

Requires ~/.focassist/credentials.json (OAuth client secret). The first run
opens a browser to consent; after that, ~/.focassist/token.json is reused.
Read-only — nothing here can send, modify, or delete mail or events.

This can also be triggered remotely via the Telegram /fetch command — see
agent/weekly_fetch.py (the orchestration logic shared by both paths) and
agent/main.py (which polls for jobs queued that way).
"""

import argparse
import sys

from agent.weekly_fetch import fetch_and_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="How many days back to fetch (default: 7)")
    parser.add_argument("--max-emails", type=int, default=200, help="Cap on emails fetched (default: 200)")
    parser.add_argument("--max-events", type=int, default=20, help="Cap on calendar events fetched (default: 20)")
    parser.add_argument("--emails-out", default="emails_last_week.json")
    parser.add_argument("--calendar-out", default="calendar_last_week.json")
    args = parser.parse_args()

    try:
        fetch_and_write(
            days=args.days,
            max_emails=args.max_emails,
            max_events=args.max_events,
            emails_out=args.emails_out,
            calendar_out=args.calendar_out,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except PermissionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
