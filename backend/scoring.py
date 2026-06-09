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

DIVIDER = "─────────────────────────────"
_BAR_LEN = 10


def _fmt(mins: float) -> str:
    h, m = int(mins) // 60, int(mins) % 60
    if h and m:
        return f"{h}h {m:02d}m"
    return f"{h}h" if h else f"{m}m"


def _bar(frac: float) -> str:
    filled = max(0, min(_BAR_LEN, round(frac * _BAR_LEN)))
    return "█" * filled + "░" * (_BAR_LEN - filled)


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

    longest     = max(
        (s["span_minutes"] if isinstance(s, dict) else s["span_minutes"] for s in sessions),
        default=0,
    )

    # Fix A: Depth uses *all* deep minutes, not just in-session deep minutes.
    # Consistency still uses the longest qualified session span so it rewards
    # sustained focus without penalising a fragmented-but-real deep-work day.
    depth       = min(deep_agg / deep_target_min,   1.0) if deep_target_min  > 0 else 0.0
    consistency = min(longest  / streak_target_min, 1.0) if streak_target_min > 0 else 0.0
    cleanliness = (1 - min(distraction / max(active, 1), 0.5) / 0.5) if active > 0 else 1.0

    score = int(100 * (0.45 * depth + 0.25 * consistency + 0.30 * cleanliness))

    return {
        "score":                   score,
        "deep_minutes":            round(deep_agg, 1),
        "active_minutes":          round(active, 1),
        "distraction_minutes":     round(distraction, 1),
        "session_count":           len(sessions),
        "longest_session_minutes": round(longest, 1),
        "depth":                   round(depth, 3),
        "consistency":             round(consistency, 3),
        "cleanliness":             round(cleanliness, 3),
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

    _BLANK = ("idle", "neutral", "untracked", "future")
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
        # Strip trailing future buckets within the current (partial) hour
        while slice_ and slice_[-1] == "future":
            slice_ = slice_[:-1]
        if not slice_:
            continue
        emojis = "".join(TIER_ICON.get(t, "⬜") for t in slice_)
        chunks.append(f"{_hour_label(hour):>4} {emojis}")
    return "\n".join(chunks)


def _top_items(aggregates: list, tier: str, n: int = 3) -> list[tuple[str, float]]:
    """Return top-n (label, minutes) items for a tier, rolled up by domain/app.
    Browser-unlabeled entries are excluded — they appear in the warning banner."""
    totals: dict[str, float] = {}
    for a in aggregates:
        a = a if isinstance(a, dict) else dict(a)
        if a["tier"] != tier:
            continue
        if a.get("category") == "browser-unlabeled":
            continue
        label = a["domain"] or a["app"]
        if label:
            totals[label] = totals.get(label, 0) + a["minutes"]
    return [(lbl, mins) for lbl, mins in sorted(totals.items(), key=lambda x: -x[1]) if mins >= 1.0][:n]


TIER_SECTION_TITLE = {
    "deep":        "Top deep work",
    "supporting":  "Top supporting",
    "distraction": "Top distractions",
}


# ── Score why-line ────────────────────────────────────────────────────────────

def _score_why(m: dict, totals: dict) -> str | None:
    if m["score"] >= 80:
        return None
    parts = []
    deep_min = totals.get("deep", 0)
    # Consistency signal
    if m["session_count"] == 0 and deep_min > 0:
        parts.append("no block ≥25 min")
    elif m["longest_session_minutes"] > 0 and m["consistency"] < 0.40:
        parts.append(f"longest block {int(m['longest_session_minutes'])}m")
    # Cleanliness signal
    active = m["active_minutes"]
    dist = totals.get("distraction", 0)
    if active > 0 and dist / active >= 0.38:
        pct = int(dist / active * 100 // 10 * 10)
        parts.append(f"~{max(pct, 10)}% distraction")
    # Depth signal (fallback when other reasons are silent)
    if not parts and deep_min < 30:
        parts.append("no deep work" if deep_min == 0 else f"only {_fmt(deep_min)} deep")
    return " · ".join(parts[:2]) if parts else None


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

    # Friendly date label
    from datetime import date as _date
    try:
        parsed = _date.fromisoformat(for_date)
        date_label = parsed.strftime("%a %b %-d")
    except Exception:
        date_label = for_date

    lines: list[str] = []

    # ── Score + why ────────────────────────────────────────────────────────────
    trend = ""
    if prev_score is not None:
        delta = m["score"] - prev_score
        arrow = "▲" if delta >= 0 else "▼"
        trend = f"   {arrow} {abs(delta)} vs yesterday"
    lines.append(f"📊 Focus {m['score']}/100{trend}")
    why = _score_why(m, totals)
    if why:
        lines.append(f"why: {why}")
    lines.append(DIVIDER)

    # ── Warnings at top — caveat everything below ──────────────────────────────
    if coverage:
        flags = coverage.get("flags", [])
        warned = False
        for flag in flags:
            ftype = flag.get("type", "")
            if ftype == "chrome_unlabeled":
                unlab = flag.get("unlabeled_minutes")
                unlab_str = _fmt(unlab) if unlab else "Some browsing"
                lines.append(f"⚠️ {unlab_str} of browsing is unlabeled — the Chrome")
                lines.append(f"   extension looks down, so categories are partial.")
                lines.append(f"   Read the breakdown loosely.")
                warned = True
            elif ftype == "non_chrome_browser":
                mins = flag.get("minutes", 0)
                lines.append(f"⚠️ {_fmt(mins)} in non-Chrome browser — no URL labels.")
                warned = True
        if warned:
            lines.append(DIVIDER)

    # ── Time accounting ────────────────────────────────────────────────────────
    first_tracked = (coverage or {}).get("first_tracked_ist")
    tracked_str = f" · tracked from {first_tracked}" if first_tracked else ""
    lines.append(f"⏱ {date_label}{tracked_str}")
    if coverage:
        idle_min = coverage.get("idle_minutes", 0)
        untracked_min = coverage.get("untracked_minutes", 0)
        lines.append(f"🟩 active    {_fmt(active):>7}")
        lines.append(f"⬜ idle      {_fmt(idle_min):>7}   away from laptop")
        lines.append(f"⬛ untracked {_fmt(untracked_min):>7}   asleep / closed")
    else:
        lines.append(f"🟩 active    {_fmt(active):>7}")
    lines.append(DIVIDER)

    # ── Tier breakdown — explicitly a child of active ──────────────────────────
    lines.append(f"📂 That active {_fmt(active)} breaks into:")
    for tier in ("deep", "supporting", "distraction"):
        mins = totals.get(tier, 0)
        icon  = TIER_ICON[tier]
        label = TIER_LABEL[tier]
        if mins > 0 and active > 0:
            frac = mins / active
            pct  = round(frac * 100)
            lines.append(f"{icon} {label:<11} {_fmt(mins):<8} {_bar(frac)} {pct}%")
        else:
            lines.append(f"{icon} {label:<11} —")
    lines.append(DIVIDER)

    # ── Timeline with legend ───────────────────────────────────────────────────
    if timeline:
        strip = _timeline_strip(timeline)
        if strip:
            lines.append("🕐 By hour  🟩deep 🟦supp 🟥distr ⬜idle ⬛untrk")
            lines.append(strip)
            lines.append(DIVIDER)

    # ── Focus sessions — explains why 0 sessions ≠ no deep work ───────────────
    n_sess = m["session_count"]
    deep_agg_min = totals.get("deep", 0)
    lines.append(f"🎯 Focus sessions: {n_sess}")
    if n_sess > 0:
        total_deep_sess = sum(
            (s["deep_minutes"] if isinstance(s, dict) else s["deep_minutes"])
            for s in sessions
        )
        best = max(
            sessions,
            key=lambda s: (s["span_minutes"] if isinstance(s, dict) else s["span_minutes"]),
        )
        best = best if isinstance(best, dict) else dict(best)
        pl = "s" if n_sess > 1 else ""
        lines.append(f"{_fmt(total_deep_sess)} deep across {n_sess} session{pl}")
        lines.append(f"Best: {_fmt_time(best['start'])}–{_fmt_time(best['end'])} ({int(best['span_minutes'])}m)")
    elif deep_agg_min >= 5:
        lines.append(f"{_fmt(deep_agg_min)} of deep work, but scattered — nothing")
        lines.append(f"ran ≥25 min unbroken. Sustained blocks are")
        lines.append(f"what lift the score.")
    else:
        lines.append("No deep work recorded.")
    lines.append(DIVIDER)

    # ── Top items per tier — capped at 3, right-aligned minutes ───────────────
    for tier in ("deep", "supporting", "distraction"):
        lines.append(f"{TIER_ICON[tier]} {TIER_SECTION_TITLE[tier]}")
        top = _top_items(aggregates, tier, 3)
        if top:
            for lbl, mins in top:
                lbl_t = lbl[:16]
                lines.append(f"   {lbl_t:<16} {_fmt(mins):>7}")
        else:
            lines.append("   —")

    # ── Coaching tip ───────────────────────────────────────────────────────────
    tip: str | None = None
    if n_sess == 0 and deep_agg_min > 0:
        tip = "Tomorrow: protect one 25-min block\nbefore messages start."
    elif n_sess > 0 and m["consistency"] < 0.50:
        tip = "Target one unbroken 90-min block tomorrow."
    else:
        top_dist = _top_items(aggregates, "distraction", 1)
        if top_dist and totals.get("distraction", 0) > totals.get("deep", 0):
            tip = f"Block {top_dist[0][0][:20]} during your\nfocus window tomorrow."
    if tip:
        lines.append(DIVIDER)
        lines.append(f"💡 {tip}")

    body = "\n".join(lines)
    return f"```\n{body}\n```"


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

    # Pre-compute the timeline slice for this hour (used for idle/untracked totals and the strip)
    h_slice: list[str] = []
    if timeline:
        per_hour = 4
        idx = (hour - 8) * per_hour
        if 0 <= idx < len(timeline):
            h_slice = timeline[idx: idx + per_hour]

    tier_totals = {t: 0.0 for t in ("deep", "supporting", "distraction", "neutral")}
    for i in items:
        tier_totals[i["tier"]] = tier_totals.get(i["tier"], 0.0) + i["minutes"]

    lines = [header, ""]
    for tier in ("deep", "supporting", "distraction", "neutral"):
        lines.append(f"{TIER_ICON[tier]} {TIER_LABEL[tier]:<12} {_fmt(tier_totals[tier])}")
    # §6: surface idle + untracked so active + idle + untracked = elapsed wall-clock.
    # Exclude "future" buckets — time that hasn't happened yet is not untracked.
    if h_slice:
        idle_min = sum(15.0 for s in h_slice if s == "idle")
        untracked_min = sum(15.0 for s in h_slice if s == "untracked")
        if idle_min > 0:
            lines.append(f"⬜ {'Idle':<12} {_fmt(idle_min)}")
        if untracked_min > 0:
            lines.append(f"⬛ {'Untracked':<12} {_fmt(untracked_min)}")
    if elapsed_min is not None:
        lines.append(f"{int(elapsed_min)}m into the hour")

    # 15-min strip for this hour — stop at future buckets
    if h_slice:
        visible = [t for t in h_slice if t != "future"]
        if visible:
            emojis = "".join(TIER_ICON.get(t, "⬜") for t in visible)
            lines.append("")
            lines.append(f"15-min: {emojis}")

    # Flat by-app/site list, tier-emoji prefixed (same name may legitimately
    # appear under multiple tiers — e.g. a whitelisted path on a domain).
    lines.append("")
    lines.append("By app/site")
    for i in sorted(items, key=lambda x: -x["minutes"]):
        if i["minutes"] < 1.0:
            continue
        if i.get("category") == "browser-unlabeled":
            label = f"{i['app']} (no URL)"
        else:
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
