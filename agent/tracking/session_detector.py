"""
Focus session detector — runs on the Mac agent (has raw AW event timestamps).

Consumes the resolved timeline from agent.tracking.timeline.resolve_timeline (the AFK-anchored
merge pipeline from docs/tracking-algorithm.md §2-3), so sessions, the 15-min report
strip, and the hourly rollup all inherit the same active/idle/untracked correctness
instead of re-deriving it from raw window events.

Session algorithm (spec §3):
  - Walk the day's resolved timeline chronologically.
  - Open a session at the first deep interval.
  - Absorb non-deep gaps < GAP_TOLERANCE_MIN (quick Slack glance, a blip of idle, etc.).
  - A non-deep gap >= GAP_TOLERANCE_MIN closes the session.
  - Session qualifies if span >= MIN_SESSION_MIN AND
    absorbed non-deep time <= MAX_ABSORBED_PCT of span.

Also produces a 15-min bucket timeline for the daily report strip.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.tracking.timeline import resolve_timeline

IST = ZoneInfo("Asia/Kolkata")

# Tunable — will eventually come from backend config table.
MIN_SESSION_MIN    = 25
GAP_TOLERANCE_MIN  = 5
MAX_ABSORBED_PCT   = 0.20
TIMELINE_BUCKET_MIN = 15
TIMELINE_START_H   = 8    # 08:00 IST
TIMELINE_END_H     = 24   # through midnight (24:00 IST) — last bucket label is "11pm"


def _resolved(events: dict, resolved: dict | None = None) -> dict:
    return resolved if resolved is not None else resolve_timeline(events)


def detect_sessions(
    events: dict,
    min_session_min: int = MIN_SESSION_MIN,
    gap_tolerance_min: int = GAP_TOLERANCE_MIN,
    max_absorbed_pct: float = MAX_ABSORBED_PCT,
    resolved: dict | None = None,
) -> list[dict]:
    """Return list of qualified focus sessions in IST HH:MM."""
    timeline = _resolved(events, resolved)["timeline"]
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

    for entry in timeline:
        ts, end, tier = entry["start"], entry["end"], entry["tier"]
        dur = (end - ts).total_seconds()
        if dur <= 0:
            continue

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


def build_timeline(events: dict, resolved: dict | None = None,
                   target_date: "date | None" = None) -> list[str]:
    """
    Return a list of state/tier strings, one per 15-min bucket, covering
    TIMELINE_START_H to TIMELINE_END_H in IST.
    Each bucket gets whichever of {tier (active) | idle | untracked} covers the
    most of that slot — idle and untracked are surfaced distinctly, never merged
    or treated as zero (tracking-algorithm.md §1).
    """
    from datetime import date as _date
    timeline = _resolved(events, resolved)["timeline"]
    bucket_count = (TIMELINE_END_H - TIMELINE_START_H) * 60 // TIMELINE_BUCKET_MIN
    bucket_sec: list[dict[str, float]] = [dict() for _ in range(bucket_count)]

    ref_date = target_date if target_date is not None else datetime.now(IST).date()
    day_start = datetime(ref_date.year, ref_date.month, ref_date.day,
                         TIMELINE_START_H, 0, tzinfo=IST)

    for entry in timeline:
        ev_start = entry["start"].astimezone(IST)
        ev_end   = entry["end"].astimezone(IST)
        label    = entry["tier"]  # "deep"/"supporting"/"distraction"/"neutral"/"idle"/"untracked"
        for i in range(bucket_count):
            b_start = day_start + timedelta(minutes=i * TIMELINE_BUCKET_MIN)
            b_end   = b_start   + timedelta(minutes=TIMELINE_BUCKET_MIN)
            overlap_start = max(ev_start, b_start)
            overlap_end   = min(ev_end,   b_end)
            overlap_sec   = (overlap_end - overlap_start).total_seconds()
            if overlap_sec > 0:
                bucket_sec[i][label] = bucket_sec[i].get(label, 0) + overlap_sec

    now_ist = datetime.now(IST)
    result = []
    for i, bucket in enumerate(bucket_sec):
        b_start = day_start + timedelta(minutes=i * TIMELINE_BUCKET_MIN)
        if b_start >= now_ist:
            result.append("future")
        elif not bucket:
            result.append("untracked")
        else:
            result.append(max(bucket, key=bucket.get))
    return result


def build_daily_aggregates(events: dict, resolved: dict | None = None) -> list[dict]:
    """
    Daily rollup of active time from the resolved AFK-anchored timeline.
    Derives aggregates from the same source as build_hourly_aggregates so
    browser intervals are represented exactly once — no window/web duplication
    (Fix B: each interval contributes exactly one {tier, category, app, domain}).
    Neutral entries are excluded (per spec §1). Browser-unlabeled intervals are
    included as distraction so the score and tier totals stay honest; the
    reporting layer hides them from top-N lists and surfaces them via the warning.
    """
    timeline = _resolved(events, resolved)["timeline"]
    agg: dict[tuple, float] = defaultdict(float)
    for entry in timeline:
        if entry["state"] != "active" or entry["tier"] == "neutral":
            continue
        key = (entry["tier"], entry["category"], entry["app"], entry["domain"])
        dur = (entry["end"] - entry["start"]).total_seconds() / 60.0
        agg[key] += dur
    return [
        {"tier": k[0], "category": k[1], "app": k[2], "domain": k[3],
         "minutes": round(v, 2)}
        for k, v in agg.items()
        if v > 0.05
    ]


def build_hourly_aggregates(events: dict, resolved: dict | None = None) -> list[dict]:
    """
    Per-hour (IST) rollup of *active* time only: [{hour, tier, category, app, domain, minutes}].
    Events spanning an hour boundary are split proportionally across hours — the
    same split must be used for the daily totals, the 15-min timeline, and here
    (tracking-algorithm.md §7) so the three views always reconcile.
    """
    timeline = _resolved(events, resolved)["timeline"]
    agg: dict[tuple, float] = defaultdict(float)

    for entry in timeline:
        if entry["state"] != "active":
            continue
        ev_start = entry["start"].astimezone(IST)
        ev_end   = entry["end"].astimezone(IST)
        cur = ev_start
        while cur < ev_end:
            hour_end = (cur.replace(minute=0, second=0, microsecond=0)
                        + timedelta(hours=1))
            seg_end = min(ev_end, hour_end)
            seg_min = (seg_end - cur).total_seconds() / 60.0
            if seg_min > 0:
                key = (cur.hour, entry["tier"], entry["category"], entry["app"], entry["domain"])
                agg[key] += seg_min
            cur = seg_end

    return [
        {"hour": k[0], "tier": k[1], "category": k[2], "app": k[3], "domain": k[4],
         "minutes": round(v, 2)}
        for k, v in agg.items()
        if v > 0.05
    ]
