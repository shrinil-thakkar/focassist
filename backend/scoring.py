"""
Focus score computation and report formatting.
Score is computed on the backend from agent-derived sessions + aggregates.
No raw events or timestamps ever reach EC2.
"""
from __future__ import annotations
import json
from datetime import date, timedelta


# ── Tier helpers ──────────────────────────────────────────────────────────────

TIER_ICON  = {"deep": "🟩", "supporting": "🟦", "distraction": "🟥", "neutral": "⬜", "idle": "⬜"}
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

def _timeline_strip(buckets: list[str], start_h: int = 8, bucket_min: int = 15) -> str:
    """Render 15-min buckets as emoji pairs with hour labels, e.g. 09|🟩🟩🟥🟥|10."""
    if not buckets:
        return ""
    per_hour = 60 // bucket_min
    chunks = []
    for hour_idx in range(len(buckets) // per_hour):
        hour = start_h + hour_idx
        slice_ = buckets[hour_idx * per_hour: (hour_idx + 1) * per_hour]
        emojis = "".join(TIER_ICON.get(t, "⬜") for t in slice_)
        chunks.append(f"`{hour:02d}` {emojis}")
    return "\n".join(chunks)


# ── Coaching insights ─────────────────────────────────────────────────────────

def _top_distraction(aggregates: list) -> str | None:
    dist = [(a["domain"] or a["app"], a["minutes"])
            for a in aggregates
            if (a["tier"] if isinstance(a, dict) else a["tier"]) == "distraction"
            and (a["domain"] if isinstance(a, dict) else a["domain"])]
    if not dist:
        return None
    top = max(dist, key=lambda x: x[1])
    return f"{top[0]} ({_fmt(top[1])})"


def _coaching(metrics: dict, aggregates: list, sessions: list, prev_score: int | None) -> list[str]:
    insights = []

    if prev_score is not None:
        delta = metrics["score"] - prev_score
        if delta >= 10:
            insights.append(f"▲ {delta} pts vs yesterday — solid improvement.")
        elif delta <= -10:
            insights.append(f"▼ {abs(delta)} pts vs yesterday. More deep work tomorrow.")

    if metrics["session_count"] == 0:
        insights.append("No focus sessions today — try scheduling a 25-min deep block.")
    elif metrics["longest_session_minutes"] >= 90:
        insights.append(f"Strong {int(metrics['longest_session_minutes'])}m focus streak — protect that block.")
    elif metrics["longest_session_minutes"] < 30 and metrics["session_count"] > 0:
        insights.append("Sessions stayed short — try batching distractions to break fewer streaks.")

    if metrics["active_minutes"] > 0:
        dist_pct = metrics["distraction_minutes"] / metrics["active_minutes"]
        if dist_pct > 0.5:
            top = _top_distraction(aggregates)
            culprit = f" ({top} was the main culprit)" if top else ""
            insights.append(f"High distraction day{culprit}.")

    return insights[:2]


# ── Daily report ──────────────────────────────────────────────────────────────

def format_daily_report(
    for_date: str,
    aggregates: list,
    sessions: list,
    timeline: list[str],
    prev_score: int | None = None,
    deep_target_min: float = 240,
    streak_target_min: float = 90,
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
        trend = f"  {'▲' if delta >= 0 else '▼'} {abs(delta)} vs yesterday"
    lines = [f"📊 *Focus Score: {m['score']}/100*{trend}"]
    lines.append(f"🗓 {for_date} · active {_fmt(active)}\n")

    # Tier breakdown
    for tier in ("deep", "supporting", "distraction", "neutral"):
        mins = totals.get(tier, 0)
        if tier == "deep" and sessions:
            sess_info = f"   {m['session_count']} session{'s' if m['session_count'] != 1 else ''} · longest {int(m['longest_session_minutes'])}m"
        else:
            sess_info = ""
        if mins > 0:
            lines.append(f"{TIER_ICON[tier]} {TIER_LABEL[tier]:<12} {_fmt(mins)}{sess_info}")
        elif tier != "neutral":
            lines.append(f"{TIER_ICON[tier]} {TIER_LABEL[tier]:<12} —")

    # Timeline strip
    if timeline:
        lines.append("")
        lines.append("*Timeline (15-min buckets):*")
        lines.append(_timeline_strip(timeline))

    # Coaching
    insights = _coaching(m, aggregates, sessions, prev_score)
    if insights:
        lines.append("")
        for ins in insights:
            lines.append(f"💡 {ins}")

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
