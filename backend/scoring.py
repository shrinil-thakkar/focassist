"""
Focus score computation and report formatting.
Score is computed on the backend from agent-derived sessions + aggregates.
No raw events or timestamps ever reach EC2.
"""
from __future__ import annotations
import json
from datetime import date, timedelta


# ── Tier helpers ──────────────────────────────────────────────────────────────

TIER_ICON  = {"deep": "🟩", "supporting": "🟦", "distraction": "🟥", "neutral": "⬜",
              "idle": "⬜", "untracked": "⬛"}
TIER_LABEL = {"deep": "Deep", "supporting": "Supporting", "distraction": "Distraction", "neutral": "Neutral"}


def _fmt(mins: float) -> str:
    h, m = int(mins) // 60, int(mins) % 60
    return f"{h}h {m}m" if h else f"{m}m"


def _tier_totals(aggregates: list) -> dict[str, float]:
    totals: dict[str, float] = {}
    for a in aggregates:
        t = a["tier"] if isinstance(a, dict) else a["tier"]
        totals[t] = totals.get(t, 0) + (a["minutes"] if isinstance(a, dict) else a["minutes"])
    return totals


# ── Focus score ───────────────────────────────────────────────────────────────

def compute_score(
    aggregates: list,
    sessions: list,
    deep_target_min: float = 240,
    streak_target_min: float = 90,
) -> dict:
    totals = _tier_totals(aggregates)
    deep_agg    = totals.get("deep", 0)
    supporting  = totals.get("supporting", 0)
    distraction = totals.get("distraction", 0)
    active      = deep_agg + supporting + distraction

    deep_sess   = sum(s["deep_minutes"]    if isinstance(s, dict) else s["deep_minutes"]    for s in sessions)
    longest     = max((s["span_minutes"]   if isinstance(s, dict) else s["span_minutes"]    for s in sessions), default=0)

    depth       = min(deep_sess / deep_target_min,   1.0) if deep_target_min  > 0 else 0.0
    consistency = min(longest  / streak_target_min,  1.0) if streak_target_min > 0 else 0.0
    cleanliness = (1 - min(distraction / max(active, 1), 0.5) / 0.5) if active > 0 else 1.0

    score = int(100 * (0.45 * depth + 0.25 * consistency + 0.30 * cleanliness))

    return {
        "score":                  score,
        "deep_minutes":           round(deep_sess, 1),
        "active_minutes":         round(active, 1),
        "distraction_minutes":    round(distraction, 1),
        "session_count":          len(sessions),
        "longest_session_minutes":round(longest, 1),
        "depth":                  round(depth, 3),
        "consistency":            round(consistency, 3),
        "cleanliness":            round(cleanliness, 3),
    }


# ── Timeline strip ────────────────────────────────────────────────────────────

def _hour_label(h: int) -> str:
    """Convert 24h integer to 12h label: 14 → '2pm', 9 → '9am'."""
    if h == 0:   return "12am"
    if h < 12:   return f"{h}am"
    if h == 12:  return "12pm"
    return f"{h - 12}pm"


def _fmt_time(hhmm: str) -> str:
    """Convert HH:MM to 12h format: '14:30' → '2:30pm', '09:00' → '9am'."""
    try:
        h, m = map(int, hhmm.split(":"))
        period = "am" if h < 12 else "pm"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d}{period}" if m else f"{h12}{period}"
    except Exception:
        return hhmm


def _timeline_strip(buckets: list[str], start_h: int = 8, bucket_min: int = 15) -> str:
    """Render 15-min buckets per hour with 12h labels, trimming idle hours."""
    if not buckets:
        return ""
    per_hour = 60 // bucket_min
    n_hours  = len(buckets) // per_hour

    _BLANK = ("idle", "neutral", "untracked")
    first_active = next(
        (h for h in range(n_hours)
         if any(b not in _BLANK for b in buckets[h*per_hour:(h+1)*per_hour])),
        None,
    )
    last_active = next(
        (h for h in range(n_hours - 1, -1, -1)
         if any(b not in _BLANK for b in buckets[h*per_hour:(h+1)*per_hour])),
        None,
    )
    if first_active is None:
        return ""

    chunks = []
    for hour_idx in range(first_active, last_active + 1):
        hour   = start_h + hour_idx
        slice_ = buckets[hour_idx * per_hour: (hour_idx + 1) * per_hour]
        emojis = "".join(TIER_ICON.get(t, "⬜") for t in slice_)
        chunks.append(f"`{_hour_label(hour)}` {emojis}")
    return "\n".join(chunks)


def _top_items(aggregates: list, tier: str, n: int = 3) -> list[tuple[str, float]]:
    """Return top-n (label, minutes) items for a tier, rolled up by domain/app."""
    totals: dict[str, float] = {}
    for a in aggregates:
        if (a["tier"] if isinstance(a, dict) else a["tier"]) != tier:
            continue
        label = (a["domain"] if isinstance(a, dict) else a["domain"]) or \
                (a["app"]    if isinstance(a, dict) else a["app"])
        if label:
            totals[label] = totals.get(label, 0) + \
                            (a["minutes"] if isinstance(a, dict) else a["minutes"])
    return sorted(totals.items(), key=lambda x: -x[1])[:n]


TIER_SECTION_TITLE = {
    "deep":        "Top deep work",
    "supporting":  "Top supporting",
    "distraction": "Top distractions",
}


# ── Daily report ──────────────────────────────────────────────────────────────

def format_daily_report(
    for_date: str,
    aggregates: list,
    sessions: list,
    timeline: list[str],
    prev_score: int | None = None,
    deep_target_min: float = 240,
    streak_target_min: float = 90,
    coverage: dict | None = None,
) -> str:
    if not aggregates and not sessions:
        return f"No activity recorded for {for_date} yet."

    m = compute_score(aggregates, sessions, deep_target_min, streak_target_min)
    totals = _tier_totals(aggregates)
    active = m["active_minutes"]

    # Headline
    trend = ""
    if prev_score is not None:
        delta = m["score"] - prev_score
        arrow = "▲" if delta >= 0 else "▼"
        trend = f"  {arrow} {abs(delta)} vs yesterday"

    # Friendly date label
    from datetime import date as _date
    try:
        parsed = _date.fromisoformat(for_date)
        today = _date.today()
        if parsed == today:
            date_label = f"Today, {parsed.strftime('%b %-d')}"
        elif (today - parsed).days == 1:
            date_label = f"Yesterday, {parsed.strftime('%b %-d')}"
        else:
            date_label = parsed.strftime("%b %-d")
    except Exception:
        date_label = for_date

    lines = [f"📊 *Focus Score: {m['score']}/100*{trend}"]

    # ── Headline + reconciliation (the trust check, tracking-algorithm.md §6) ──
    # active + idle + untracked must equal elapsed wall-clock; fold all three
    # into one line — using the same icons as the strip below — so a
    # half-tracked day reads as half-tracked, never as a quiet/lazy one.
    if coverage:
        lines.append(
            f"🗓 {date_label}\n"
            f"🟩 active {_fmt(active)}"
            f"   ⬜ idle {_fmt(coverage.get('idle_minutes', 0))}"
            f"   ⬛ untracked {_fmt(coverage.get('untracked_minutes', 0))}"
        )
        for flag in coverage.get("flags", []):
            lines.append(f"⚠️ {flag.get('message', flag.get('type', ''))}")
    else:
        lines.append(f"🗓 {date_label} · active {_fmt(active)}")
    lines.append("")

    # Tier breakdown (no inline extras — dedicated sections below)
    for tier in ("deep", "supporting", "distraction", "neutral"):
        mins = totals.get(tier, 0)
        if mins > 0:
            lines.append(f"{TIER_ICON[tier]} {TIER_LABEL[tier]:<12} {_fmt(mins)}")
        elif tier not in ("neutral", "supporting"):
            lines.append(f"{TIER_ICON[tier]} {TIER_LABEL[tier]:<12} —")

    # Timeline strip
    if timeline:
        strip = _timeline_strip(timeline)
        if strip:
            lines.append("")
            lines.append(strip)

    # ── Focus sessions section ────────────────────────────────────────────────
    lines.append("")
    n_sess = m["session_count"]
    deep_agg = totals.get("deep", 0)

    if n_sess > 0:
        total_deep_sess = sum(
            (s["deep_minutes"] if isinstance(s, dict) else s["deep_minutes"])
            for s in sessions
        )
        best = max(sessions,
                   key=lambda s: (s["span_minutes"] if isinstance(s, dict) else s["span_minutes"]))
        best = best if isinstance(best, dict) else dict(best)
        lines.append(
            f"🎯 *Focus sessions: {n_sess}*  ·  {_fmt(total_deep_sess)} deep"
        )
        lines.append(
            f"   Best: {_fmt_time(best['start'])} – {_fmt_time(best['end'])}  "
            f"({int(best['span_minutes'])}m)"
        )
    elif deep_agg >= 5:
        lines.append(f"🎯 *Focus sessions: 0*")
        lines.append(f"   {_fmt(deep_agg)} deep done — fragmented, no 25-min block")
    else:
        lines.append("🎯 *Focus sessions: 0*  — no deep work recorded")

    # ── Top items per tier ────────────────────────────────────────────────────
    for tier in ("deep", "supporting", "distraction"):
        top3 = _top_items(aggregates, tier, 3)
        if top3:
            lines.append("")
            lines.append(f"{TIER_ICON[tier]} *{TIER_SECTION_TITLE[tier]}*")
            for label, mins in top3:
                lines.append(f"   {label:<28} {_fmt(mins)}")

    return "\n".join(lines)


# ── Hour report ───────────────────────────────────────────────────────────────

def format_hour_report(
    for_date: str,
    hour: int,
    items: list,
    timeline: list[str] | None = None,
    sessions: list | None = None,
    elapsed_min: float | None = None,
) -> str:
    """
    Detailed app/site breakdown for a single hour of the day.
    `elapsed_min` is set only for the current (partial) hour — minutes since :00.
    """
    h_start = f"{hour:02d}:00"
    h_end   = f"{(hour + 1) % 24:02d}:00"
    so_far  = "  (so far)" if elapsed_min is not None else ""
    header  = f"🕐 *{h_start}–{h_end}*  ·  {for_date}{so_far}"

    if not items:
        # Distinguish idle (laptop on, away) from untracked (asleep/crashed/no
        # data) — §1's cardinal rule: never merge them, never read either as zero.
        label = "idle or laptop closed"
        if timeline:
            per_hour = 4
            idx = (hour - 8) * per_hour
            if 0 <= idx < len(timeline):
                slice_ = timeline[idx: idx + per_hour]
                if slice_ and all(s == "untracked" for s in slice_):
                    label = "untracked — asleep, watcher down, or no data"
                elif slice_ and all(s in ("idle", "untracked") for s in slice_):
                    label = "idle/untracked — away from the laptop"
        return f"{header}\n\nNothing tracked {h_start}–{h_end} — {label}."

    items = [dict(i) for i in items]

    tier_totals = {t: 0.0 for t in ("deep", "supporting", "distraction", "neutral")}
    for i in items:
        tier_totals[i["tier"]] = tier_totals.get(i["tier"], 0.0) + i["minutes"]

    lines = [header, ""]
    for tier in ("deep", "supporting", "distraction", "neutral"):
        lines.append(f"{TIER_ICON[tier]} {TIER_LABEL[tier]:<12} {_fmt(tier_totals[tier])}")
    if elapsed_min is not None:
        lines.append(f"{int(elapsed_min)}m into the hour")

    # 15-min strip for this hour
    if timeline:
        per_hour = 4
        idx = (hour - 8) * per_hour
        if 0 <= idx < len(timeline):
            slice_ = timeline[idx: idx + per_hour]
            emojis = "".join(TIER_ICON.get(t, "⬜") for t in slice_)
            lines.append("")
            lines.append(f"15-min: {emojis}")

    # Flat by-app/site list, tier-emoji prefixed (same name may legitimately
    # appear under multiple tiers — e.g. a whitelisted path on a domain).
    lines.append("")
    lines.append("By app/site")
    for i in sorted(items, key=lambda x: -x["minutes"]):
        label = i["domain"] or i["app"]
        lines.append(f"{TIER_ICON[i['tier']]} {label:<24} {_fmt(i['minutes'])}")

    # Overlapping focus session
    if sessions:
        for s in sessions:
            s = s if isinstance(s, dict) else dict(s)
            if s["start"] < h_end and s["end"] > h_start:
                lines.append("")
                lines.append(
                    f"⏱ Focus session {s['start']}–{s['end']} overlaps this hour (deep ✓)"
                )
                break

    return "\n".join(lines)


# ── Weekly report ─────────────────────────────────────────────────────────────

def format_weekly_report(
    days: list[dict],   # [{date, aggregates, sessions}] newest-last
    deep_target_min: float = 240,
    streak_target_min: float = 90,
) -> str:
    if not days:
        return "No data recorded this week yet."

    scores = []
    lines_per_day = []
    best_date = best_score = None

    for day in days:
        m = compute_score(day["aggregates"], day["sessions"], deep_target_min, streak_target_min)
        scores.append(m["score"])
        bar_full  = m["score"] * 10 // 100
        bar_empty = 10 - bar_full
        bar = "▓" * bar_full + "░" * bar_empty
        lines_per_day.append(f"`{day['date'][5:]}` {bar} *{m['score']}*")
        if best_score is None or m["score"] > best_score:
            best_score = m["score"]
            best_date  = day["date"]

    avg = int(sum(scores) / len(scores)) if scores else 0
    prev_avg = None  # would need two weeks of data for trend

    # Header
    start_d = days[0]["date"][5:]
    end_d   = days[-1]["date"][5:]
    lines = [f"📈 *Week {start_d} – {end_d}*"]
    lines.append(f"Avg Focus *{avg}*   ·   Best day {best_date[5:] if best_date else '—'} ({best_score})\n")
    lines.extend(lines_per_day)

    # Weekly tier totals
    all_aggs = [a for day in days for a in day["aggregates"]]
    totals   = _tier_totals(all_aggs)
    all_sess = [s for day in days for s in day["sessions"]]
    total_deep = sum(s["deep_minutes"] if isinstance(s, dict) else s["deep_minutes"] for s in all_sess)

    lines.append("")
    lines.append(
        f"Deep *{_fmt(total_deep)}*  ·  "
        f"Supporting *{_fmt(totals.get('supporting', 0))}*  ·  "
        f"Distraction *{_fmt(totals.get('distraction', 0))}*"
    )

    # Top distractions
    dist_by_domain: dict[str, float] = {}
    for a in all_aggs:
        if (a["tier"] if isinstance(a, dict) else a["tier"]) == "distraction":
            d = a["domain"] if isinstance(a, dict) else a["domain"]
            if d:
                dist_by_domain[d] = dist_by_domain.get(d, 0) + (a["minutes"] if isinstance(a, dict) else a["minutes"])
    if dist_by_domain:
        top3 = sorted(dist_by_domain.items(), key=lambda x: -x[1])[:3]
        lines.append("Top distractions: " + "  ·  ".join(f"{d} {_fmt(m)}" for d, m in top3))

    # Best session
    best_sess = max(all_sess, key=lambda s: (s["span_minutes"] if isinstance(s, dict) else s["span_minutes"]), default=None)
    if best_sess:
        bs = best_sess if isinstance(best_sess, dict) else dict(best_sess)
        lines.append(f"Best session: {bs['start']}–{bs['end']} ({int(bs['span_minutes'])}m deep)")

    return "\n".join(lines)
