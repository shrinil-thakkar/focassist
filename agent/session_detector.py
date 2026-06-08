"""
Focus session detector — runs on the Mac agent (has raw AW event timestamps).

Algorithm (from spec §3):
  - Walk the day's tiered event timeline chronologically.
  - Open a session at the first deep event.
  - Absorb non-deep gaps < GAP_TOLERANCE_MIN (quick Slack glance etc.).
  - A non-deep gap >= GAP_TOLERANCE_MIN closes the session.
  - Session qualifies if span >= MIN_SESSION_MIN AND
    absorbed non-deep time <= MAX_ABSORBED_PCT of span.

Also produces a 15-min bucket timeline for the daily report strip.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Tunable — will eventually come from backend config table.
MIN_SESSION_MIN    = 25
GAP_TOLERANCE_MIN  = 5
MAX_ABSORBED_PCT   = 0.20
TIMELINE_BUCKET_MIN = 15
TIMELINE_START_H   = 8    # 08:00 IST
TIMELINE_END_H     = 22   # 22:00 IST


def _tier_timeline(events: dict) -> list[tuple[datetime, float, str, str, str, str]]:
    """
    Build a sorted list of (utc_start, duration_sec, tier, category, app, domain)
    from raw AW events. Browser window events are skipped — web events are used
    for browser time. Neutral events are included (needed for gap detection) but
    won't open sessions.
    """
    from agent.categorizer import classify, BROWSER_APPS, _extract_domain

    entries: list[tuple[datetime, float, str, str, str, str]] = []

    for ev in events.get("window", []):
        data = ev.get("data", {})
        app  = data.get("app", "")
        dur  = ev.get("duration", 0)
        if dur < 1 or app in BROWSER_APPS:
            continue
        rule = classify(app, "", "")
        tier = rule["tier"] if rule else "distraction"
        category = rule["category"] if rule else "other"
        ts = datetime.fromisoformat(ev["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        entries.append((ts, dur, tier, category, app, ""))

    for ev in events.get("web", []):
        data   = ev.get("data", {})
        url    = data.get("url", "")
        dur    = ev.get("duration", 0)
        if dur < 1:
            continue
        domain = _extract_domain(url)
        if not domain or domain.startswith("chrome") or domain.startswith("about"):
            continue
        app = data.get("app", "Browser")
        rule = classify(app, domain, url)
        tier = rule["tier"] if rule else "distraction"
        category = rule["category"] if rule else "browsing"
        ts = datetime.fromisoformat(ev["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        entries.append((ts, dur, tier, category, app, domain))

    return sorted(entries, key=lambda x: x[0])


def detect_sessions(
    events: dict,
    min_session_min: int = MIN_SESSION_MIN,
    gap_tolerance_min: int = GAP_TOLERANCE_MIN,
    max_absorbed_pct: float = MAX_ABSORBED_PCT,
) -> list[dict]:
    """Return list of qualified focus sessions in IST HH:MM."""
    timeline = _tier_timeline(events)
    if not timeline:
        return []

    gap_sec = gap_tolerance_min * 60
    sessions = []

    session_start: datetime | None = None
    deep_sec   = 0.0
    absorbed_sec = 0.0
    last_deep_end: datetime | None = None

    def _close(end_dt: datetime) -> None:
        nonlocal session_start, deep_sec, absorbed_sec, last_deep_end
        if session_start is None:
            return
        span_sec = (end_dt - session_start).total_seconds()
        deep_min = deep_sec / 60
        abs_min  = absorbed_sec / 60
        span_min = span_sec / 60
        if (deep_min >= min_session_min
                and span_sec > 0
                and absorbed_sec / span_sec <= max_absorbed_pct):
            sessions.append({
                "start":            session_start.astimezone(IST).strftime("%H:%M"),
                "end":              end_dt.astimezone(IST).strftime("%H:%M"),
                "deep_minutes":     round(deep_min, 1),
                "absorbed_minutes": round(abs_min, 1),
                "span_minutes":     round(span_min, 1),
            })
        session_start  = None
        deep_sec       = 0.0
        absorbed_sec   = 0.0
        last_deep_end  = None

    for ts, dur, tier, *_ in timeline:
        end = ts + timedelta(seconds=dur)

        if tier == "deep":
            if session_start is None:
                session_start = ts
                deep_sec      = dur
                absorbed_sec  = 0.0
                last_deep_end = end
            else:
                gap = (ts - last_deep_end).total_seconds()
                if gap <= gap_sec:
                    deep_sec     += dur
                    absorbed_sec += max(gap, 0)
                    last_deep_end = end
                else:
                    _close(last_deep_end)
                    session_start = ts
                    deep_sec      = dur
                    absorbed_sec  = 0.0
                    last_deep_end = end
        elif session_start is not None:
            gap = (ts - last_deep_end).total_seconds()
            if gap > gap_sec:
                _close(last_deep_end)

    if session_start is not None:
        _close(last_deep_end)

    return sessions


def build_timeline(events: dict) -> list[str]:
    """
    Return a list of tier strings, one per 15-min bucket, covering
    TIMELINE_START_H to TIMELINE_END_H in IST.
    Each bucket gets the tier of whichever event covers the most of that slot.
    """
    raw = _tier_timeline(events)
    bucket_count = (TIMELINE_END_H - TIMELINE_START_H) * 60 // TIMELINE_BUCKET_MIN
    bucket_sec: list[dict[str, float]] = [dict() for _ in range(bucket_count)]

    today_ist = datetime.now(IST).date()
    day_start = datetime(today_ist.year, today_ist.month, today_ist.day,
                         TIMELINE_START_H, 0, tzinfo=IST)

    for ts, dur, tier, *_ in raw:
        ev_start = ts.astimezone(IST)
        ev_end   = ev_start + timedelta(seconds=dur)
        for i in range(bucket_count):
            b_start = day_start + timedelta(minutes=i * TIMELINE_BUCKET_MIN)
            b_end   = b_start   + timedelta(minutes=TIMELINE_BUCKET_MIN)
            overlap_start = max(ev_start, b_start)
            overlap_end   = min(ev_end,   b_end)
            overlap_sec   = (overlap_end - overlap_start).total_seconds()
            if overlap_sec > 0:
                bucket_sec[i][tier] = bucket_sec[i].get(tier, 0) + overlap_sec

    result = []
    for bucket in bucket_sec:
        if not bucket:
            result.append("idle")
        else:
            result.append(max(bucket, key=bucket.get))
    return result


def build_hourly_aggregates(events: dict) -> list[dict]:
    """
    Per-hour (IST) rollup: [{hour, tier, category, app, domain, minutes}].
    Events spanning an hour boundary are split proportionally across hours.
    """
    raw = _tier_timeline(events)
    agg: dict[tuple, float] = defaultdict(float)

    for ts, dur, tier, category, app, domain in raw:
        ev_start = ts.astimezone(IST)
        ev_end   = ev_start + timedelta(seconds=dur)
        cur = ev_start
        while cur < ev_end:
            hour_end = (cur.replace(minute=0, second=0, microsecond=0)
                        + timedelta(hours=1))
            seg_end = min(ev_end, hour_end)
            seg_min = (seg_end - cur).total_seconds() / 60.0
            if seg_min > 0:
                agg[(cur.hour, tier, category, app, domain)] += seg_min
            cur = seg_end

    return [
        {"hour": k[0], "tier": k[1], "category": k[2], "app": k[3], "domain": k[4],
         "minutes": round(v, 2)}
        for k, v in agg.items()
        if v > 0.05
    ]
