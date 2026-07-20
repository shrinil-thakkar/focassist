"""CLI: eyeball today's calendar and free gaps.

The actual calendar logic lives in agent/google/calendar_client.py — this is
just the `python -m agent.calendar` entry point that renders it.
"""

import datetime as dt

from agent.google.calendar_client import DAY_END, DAY_START, get_today_context


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%H:%M")


def main():
    ctx = get_today_context()
    print(f"{ctx.date:%A, %d %b %Y}\n")

    print("Fixed today:")
    if not ctx.events:
        print("  (nothing on the calendar)")
    for e in ctx.events:
        if e.all_day:
            print(f"  all-day      {e.summary}")
        else:
            print(f"  {_fmt(e.start)}-{_fmt(e.end)}  {e.summary}")

    print(f"\nFree gaps ({DAY_START:%H:%M}-{DAY_END:%H:%M}):")
    if not ctx.gaps:
        print("  (none — fully booked)")
    for g in ctx.gaps:
        h, m = divmod(g.minutes, 60)
        print(f"  {_fmt(g.start)}-{_fmt(g.end)}  ({h}h{m:02d})")

    h, m = divmod(ctx.discretionary_minutes, 60)
    print(f"\nDiscretionary time: {h}h{m:02d}")


if __name__ == "__main__":
    main()
