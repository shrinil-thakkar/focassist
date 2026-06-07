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
    """Return the first bucket whose id starts with `bucket_type` for hostname."""
    buckets = _get("/api/0/buckets")
    prefix = f"{bucket_type}_{hostname}"
    for bid in buckets:
        if bid.startswith(prefix) or bucket_type in bid:
            return bid
    return None


def fetch_events(target_date: date | None = None) -> dict:
    """
    Fetch today's (or target_date's) window-watcher and web-watcher events.
    Returns {"window": [...], "web": [...]} raw AW event dicts.
    """
    if target_date is None:
        target_date = date.today()

    hostname = AW_HOSTNAME or _detect_hostname()

    start = datetime(target_date.year, target_date.month, target_date.day,
                     tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime(target_date.year, target_date.month, target_date.day,
                   23, 59, 59, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    window_bucket = _bucket_id(hostname, "aw-watcher-window")
    web_bucket = _bucket_id(hostname, "aw-watcher-web")

    window_events = []
    web_events = []

    if window_bucket:
        result = _get(f"/api/0/buckets/{window_bucket}/events?start={start}&end={end}&limit=10000")
        window_events = result if isinstance(result, list) else []
    else:
        log.warning("No window-watcher bucket found. Is ActivityWatch running?")

    if web_bucket:
        result = _get(f"/api/0/buckets/{web_bucket}/events?start={start}&end={end}&limit=10000")
        web_events = result if isinstance(result, list) else []

    return {"window": window_events, "web": web_events}


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
