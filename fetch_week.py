"""Fetch the last N days of Gmail + Google Calendar data as JSON, then label
the emails automatically (rules first, Gemini for the rest — see
agent/labeling/). Pass --no-label to skip labeling and just fetch.

Usage: python fetch_week.py [--days 7]

Requires ~/.focassist/credentials.json (OAuth client secret). The first run
opens a browser to consent; after that, ~/.focassist/token.json is reused.
Read-only — nothing here can send, modify, or delete mail or events.

This can also be triggered remotely via the Telegram /fetch command — see
agent/google/weekly_fetch.py (the fetch orchestration shared by both paths)
and agent/main.py (which polls for jobs queued that way and labels
automatically too).
"""

import argparse
import json
import sys

from agent.google.weekly_fetch import fetch_and_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="How many days back to fetch (default: 7)")
    parser.add_argument("--max-emails", type=int, default=200, help="Cap on emails fetched (default: 200)")
    parser.add_argument("--max-events", type=int, default=20, help="Cap on calendar events fetched (default: 20)")
    parser.add_argument("--emails-out", default="emails_last_week.json")
    parser.add_argument("--calendar-out", default="calendar_last_week.json")
    parser.add_argument("--no-label", action="store_true", help="Skip automatic labeling of fetched emails")
    parser.add_argument("--labels-out", default="emails_labeled.json")
    parser.add_argument("--no-cache", action="store_true",
                         help="Force fresh LLM calls instead of reusing cached labels")
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

    if not args.no_label:
        from agent.label_tool import label_batch, print_summary

        with open(args.emails_out) as f:
            emails = json.load(f)
        labeled = label_batch(emails, use_cache=not args.no_cache)
        with open(args.labels_out, "w") as f:
            json.dump(labeled, f, indent=2)
        print_summary(labeled)
        print(f"\nWrote {len(labeled)} labeled emails to {args.labels_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
