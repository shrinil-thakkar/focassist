"""Calendar tool — the deterministic data foundation for the planner.

Pulls today's events from Google Calendar and computes the free gaps inside
your plannable day. There is no AI here on purpose: reading the calendar and
finding open time is pure arithmetic. The judgment part (fitting tasks into
those gaps sensibly) comes later, in the planner agent, which consumes the
DayContext this module produces.

Requires Python 3.9+ (uses zoneinfo).
"""

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# --- config -----------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

_DIR = Path(os.environ.get("FOCASSIST_DIR", Path.home() / ".focassist"))
CREDENTIALS_FILE = str(_DIR / "credentials.json")
TOKEN_FILE = str(_DIR / "token.json")

TIMEZONE = ZoneInfo("Asia/Kolkata")   # your local timezone
DAY_START = dt.time(9, 0)             # earliest hour you'd plan work into
DAY_END = dt.time(21, 0)              # latest hour you'd plan work into
MIN_GAP_MINUTES = 15                  # ignore free slivers shorter than this


# --- auth (same flow you already authorized) --------------------------------
def get_credentials():
    _DIR.mkdir(parents=True, exist_ok=True)
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


# --- data shapes the planner will consume -----------------------------------
@dataclass
class Event:
    summary: str
    start: dt.datetime
    end: dt.datetime
    all_day: bool


@dataclass
class Gap:
    start: dt.datetime
    end: dt.datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class DayContext:
    date: dt.date
    events: list   # list[Event]
    gaps: list     # list[Gap]

    @property
    def discretionary_minutes(self) -> int:
        return sum(g.minutes for g in self.gaps)


# --- fetching ---------------------------------------------------------------
def _is_declined(event: dict) -> bool:
    """True if you (the calendar owner) declined this event."""
    for a in event.get("attendees", []):
        if a.get("self") and a.get("responseStatus") == "declined":
            return True
    return False


def fetch_events(service, day: dt.date):
    """Pull everything that touches `day` (full local day), declined removed."""
    day_start = dt.datetime.combine(day, dt.time.min, tzinfo=TIMEZONE)
    day_end = dt.datetime.combine(day, dt.time.max, tzinfo=TIMEZONE)

    raw = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )

    events = []
    for e in raw:
        if _is_declined(e):
            continue
        if "dateTime" in e["start"]:
            start = dt.datetime.fromisoformat(e["start"]["dateTime"]).astimezone(TIMEZONE)
            end = dt.datetime.fromisoformat(e["end"]["dateTime"]).astimezone(TIMEZONE)
            all_day = False
        else:  # all-day event: only a `date` is present (end is exclusive)
            start = dt.datetime.combine(
                dt.date.fromisoformat(e["start"]["date"]), dt.time.min, tzinfo=TIMEZONE
            )
            end = dt.datetime.combine(
                dt.date.fromisoformat(e["end"]["date"]), dt.time.min, tzinfo=TIMEZONE
            )
            all_day = True
        events.append(Event(e.get("summary", "(no title)"), start, end, all_day))
    return events


# --- gap math (pure, deterministic) -----------------------------------------
def compute_gaps(events, day: dt.date):
    window_start = dt.datetime.combine(day, DAY_START, tzinfo=TIMEZONE)
    window_end = dt.datetime.combine(day, DAY_END, tzinfo=TIMEZONE)

    # Only timed events block specific hours. All-day events (WFH, birthdays)
    # are surfaced in the context but don't consume plannable time.
    busy = [(e.start, e.end) for e in events if not e.all_day]

    # clip to the plannable window, drop anything outside it
    clipped = []
    for s, en in busy:
        s, en = max(s, window_start), min(en, window_end)
        if s < en:
            clipped.append((s, en))

    # merge overlapping / touching intervals
    clipped.sort()
    merged = []
    for s, en in clipped:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        else:
            merged.append((s, en))

    # collect the holes between the busy blocks
    gaps, cursor = [], window_start
    for s, en in merged:
        if s - cursor >= dt.timedelta(minutes=MIN_GAP_MINUTES):
            gaps.append(Gap(cursor, s))
        cursor = max(cursor, en)
    if window_end - cursor >= dt.timedelta(minutes=MIN_GAP_MINUTES):
        gaps.append(Gap(cursor, window_end))

    return gaps


# --- public entry point -----------------------------------------------------
def get_today_context() -> DayContext:
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)
    today = dt.datetime.now(TIMEZONE).date()
    events = fetch_events(service, today)
    gaps = compute_gaps(events, today)
    return DayContext(date=today, events=events, gaps=gaps)


# --- run it to eyeball today ------------------------------------------------
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
