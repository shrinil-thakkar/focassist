"""Reads activity events from local ActivityWatch API and computes aggregates."""
import os
import sys
import logging
from datetime import date, datetime, timezone
from collections import defaultdict
from urllib.request import urlopen, Request
from urllib.error import URLError
import json

AW_BASE = os.environ.get("AW_BASE", "http://localhost:5600")
AW_HOSTNAME = os.environ.get("AW_HOSTNAME", "")  # auto-detect if empty

log = logging.getLogger(__name__)


def _get(path: str) -> dict | list:
    req = Request(f"{AW_BASE}{path}", headers={"Accept": "application/json"})
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _detect_hostname() -> str:
    info = _get("/api/0/info")
    return info["hostname"]


def _bucket_id(hostname: str, bucket_type: str) -> str | None:
    """
    Return the best-matching bucket for this hostname and type.
    Prefers buckets that contain the hostname (e.g. aw-watcher-web-chrome_HOSTNAME)
    over generic ones (e.g. aw-watcher-web-chrome with no hostname suffix).
    """
    buckets = _get("/api/0/buckets")
    # Pass 1: must start with bucket_type AND contain hostname
    for bid in buckets:
        if bid.startswith(bucket_type) and hostname in bid:
            return bid
    # Pass 2: fallback — any bucket that starts with the type prefix
    for bid in buckets:
        if bid.startswith(bucket_type):
            return bid
    return None


def fetch_events(target_date: date | None = None) -> dict:
    """
    Fetch today's (or target_date's) AFK, window-watcher and web-watcher events.
    Returns {"afk": [...], "window": [...], "web": [...]} raw AW event dicts.

    AFK is the master clock (see docs/tracking-algorithm.md §2) — it is fetched
    first and is the only bucket whose absence is fatal to honest accounting.
    """
    if target_date is None:
        target_date = date.today()

    hostname = AW_HOSTNAME or _detect_hostname()

    # Use IST-aligned day boundaries so the report matches the IST calendar day.
    # IST midnight = UTC 18:30 previous day; querying UTC-midnight would miss the
    # first 5.5 hours of the IST day.
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    start = datetime(target_date.year, target_date.month, target_date.day,
                     0, 0, 0, tzinfo=IST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime(target_date.year, target_date.month, target_date.day,
                   23, 59, 59, tzinfo=IST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    afk_bucket = _bucket_id(hostname, "aw-watcher-afk")
    window_bucket = _bucket_id(hostname, "aw-watcher-window")
    web_bucket = _bucket_id(hostname, "aw-watcher-web")

    afk_events = []
    window_events = []
    web_events = []

    if afk_bucket:
        result = _get(f"/api/0/buckets/{afk_bucket}/events?start={start}&end={end}&limit=10000")
        afk_events = result if isinstance(result, list) else []
    else:
        log.warning("No AFK-watcher bucket found — active/idle/untracked split will be unreliable.")

    if window_bucket:
        result = _get(f"/api/0/buckets/{window_bucket}/events?start={start}&end={end}&limit=10000")
        window_events = result if isinstance(result, list) else []
    else:
        log.warning("No window-watcher bucket found. Is ActivityWatch running?")

    if web_bucket:
        result = _get(f"/api/0/buckets/{web_bucket}/events?start={start}&end={end}&limit=10000")
        web_events = result if isinstance(result, list) else []

    return {"afk": afk_events, "window": window_events, "web": web_events}


def compute_aggregates(events: dict) -> tuple[list[dict], list[dict]]:
    """
    Apply Tier-1 rules to events. Returns (aggregates, ambiguous_items).
    aggregates: [{category, app, domain, minutes}]
    ambiguous:  [{app, domain, title, minutes}]

    Tier-1 rules are fetched lazily from the backend via sync.get_rules().
    This module only deals with raw roll-up; categorizer.py applies the rules.
    """
    from agent.categorizer import categorize_events
    return categorize_events(events)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        events = fetch_events()
        aggs, ambiguous = compute_aggregates(events)
        print(f"Aggregates ({len(aggs)}):")
        for a in aggs[:10]:
            print(f"  {a['category']:20s} {a['app']:30s} {a['minutes']:.1f} min")
        print(f"Ambiguous ({len(ambiguous)}):")
        for a in ambiguous[:5]:
            print(f"  {a['app']:30s} {a['domain']:30s} {a['minutes']:.1f} min")
    except URLError as e:
        print(f"Cannot reach ActivityWatch at {AW_BASE}: {e}", file=sys.stderr)
        sys.exit(1)
