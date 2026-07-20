"""
Timeline resolution — the merge pipeline from docs/tracking-algorithm.md §2-3.

AFK is the master clock. This module turns raw {afk, window, web} AW events into
a single gapless, non-overlapping list of intervals, each tagged with one of the
three honest states (active / idle / untracked) and, for active intervals, the
resolved {tier, category, app, domain}.

Sessions, the 15-min report timeline, and hourly aggregates all consume this
output so they inherit correctness instead of each re-deriving the AFK split
(see §9 — this is the highest-priority correctness fix).
"""
import re
from datetime import datetime, timedelta, timezone

DEFAULT_CONFIG = {
    # input gap before "away" — confirm against the installed aw-watcher-afk value
    "afk_timeout_sec": 180,
    # meeting apps whose focus during an afk stretch should count as active (§3)
    "engaged_apps": ["Zoom", "Microsoft Teams"],
    "engaged_domains": ["meet.google.com"],
    # video domains that only count as engaged when the *resolved* tier is deep
    # (i.e. a channel you've explicitly whitelisted — never bare autoplay)
    "engaged_video_domains": ["youtube.com"],
    # max continuous passive-engagement override; excess reverts to idle
    "override_cap_minutes": 45,
    # anything focused that looks like a browser but isn't here → flagged (§5)
    # Chrome PWAs (Telegram Web, Titan, etc.) are NOT listed here — the web extension
    # cannot inject into PWA windows, so they would always be browser-unknown.
    # They are classified directly by app name via categorizer rules instead.
    "browser_app_names": ["Google Chrome", "Google Chrome Canary"],
    # Chrome-focused URL coverage below this over a rolling window → flag (§5)
    "url_coverage_flag_threshold": 0.5,
    # URL scheme prefixes that map to neutral/system rather than browser-unknown.
    # aw-watcher-web reports these via the tabs API (Step-0 "good case"); classify
    # them as neutral so they don't inflate distraction or trigger coverage warnings.
    "neutral_url_schemes": ["chrome://", "chrome-extension://", "about:"],
    # Minimum minutes of *real* (non-internal) Chrome browsing before the
    # chrome_unlabeled warning can fire.  A day heavy on chrome:// / new-tab time
    # but light on actual browsing must not trip the extension-down alert.
    "liveness_min_minutes": 20,
    # Minimum seconds a Chrome window-focus span must last to be treated as real
    # browsing. Shorter spans are "flickers" from app-switch noise → neutral.
    "min_dwell_seconds": 3,
    # Unambiguous page-title substrings → domain. Only add patterns where the
    # suffix cannot appear in unrelated page titles (localization risk).
    # Patterns matched against the Chrome-suffix-stripped title.
    # Keep only brand-name substrings that cannot appear on unrelated pages.
    "title_to_domain": {
        "YouTube":       "youtube.com",
        "Gmail":         "mail.google.com",
        "GitHub":        "github.com",
        "Stack Overflow":"stackoverflow.com",
        "Google Docs":   "docs.google.com",
        " - Claude":     "claude.ai",
    },
    # Minimum minutes of browser-unknown (genuinely unclassifiable) residue before
    # the coverage warning fires.
    "unknown_warn_minutes": 5,
}

_UNTRACKED_TIER = "untracked"
_IDLE_TIER = "idle"


def _ts(ev: dict) -> datetime:
    ts = datetime.fromisoformat(ev["timestamp"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _span(ev: dict) -> tuple[datetime, datetime]:
    start = _ts(ev)
    return start, start + timedelta(seconds=ev.get("duration", 0) or 0)


def _overlap(a_start, a_end, b_start, b_end):
    s = max(a_start, b_start)
    e = min(a_end, b_end)
    return (s, e) if e > s else None


# ── §2 step 1 — partition the day from AFK ───────────────────────────────────

def partition_afk(afk_events: list[dict], range_start: datetime, range_end: datetime) -> list[tuple]:
    """
    Return a sorted, gapless, non-overlapping list of (start, end, base_state)
    covering [range_start, range_end), base_state in {"not-afk", "afk", "untracked"}.

    Any wall-clock interval with no AFK events at all is "untracked" — sleep,
    lid closed, watcher down, or a crash. The watchers never hand us this third
    state; we have to construct it explicitly.
    """
    raw = []
    for ev in sorted(afk_events, key=lambda e: e.get("timestamp", "")):
        start, end = _span(ev)
        ov = _overlap(start, end, range_start, range_end)
        if not ov:
            continue
        status = ev.get("data", {}).get("status", "afk")
        state = "not-afk" if status == "not-afk" else "afk"
        raw.append((ov[0], ov[1], state))

    raw.sort(key=lambda iv: iv[0])

    out: list[tuple] = []
    cursor = range_start
    for start, end, state in raw:
        if start > cursor:
            out.append((cursor, start, "untracked"))
        if end > cursor:
            out.append((max(start, cursor), end, state))
            cursor = end
    if cursor < range_end:
        out.append((cursor, range_end, "untracked"))

    return [iv for iv in out if iv[1] > iv[0]]


# ── §2 step 2 — window ∩ not-afk → active-app timeline ───────────────────────

# Strips "… - Google Chrome" / "… – Google Chrome – Profile N" from window titles.
_CHROME_SUFFIX_RE = re.compile(r'\s*[-–]\s*Google Chrome(?:\s*[-–].*)?$')


def _title_domain(title: str, cfg: dict) -> str | None:
    """
    Return a domain matched from a Chrome window title, or None if no
    confident pattern fires.  Strips the macOS Chrome suffix first, then
    checks cfg['title_to_domain'] substring patterns against the remainder.
    """
    if not title:
        return None
    clean = _CHROME_SUFFIX_RE.sub('', title).strip()
    for pattern, domain in cfg.get("title_to_domain", {}).items():
        if pattern in clean:
            return domain
    return None


def _classify_app(app: str) -> tuple[str, str]:
    from agent.tracking.categorizer import classify
    rule = classify(app, "", "")
    if rule:
        return rule["tier"], rule["category"]
    return "distraction", "other"


def _active_app_segments(window_events: list[dict], not_afk: list[tuple]) -> list[dict]:
    """Clip window events to the not-afk intervals — kills 'idle with VS Code open'."""
    segments = []
    for ev in window_events:
        data = ev.get("data", {})
        app = data.get("app", "")
        ev_start, ev_end = _span(ev)
        if ev_end <= ev_start:
            continue
        for na_start, na_end, _ in not_afk:
            ov = _overlap(ev_start, ev_end, na_start, na_end)
            if not ov:
                continue
            tier, category = _classify_app(app)
            segments.append({
                "start": ov[0], "end": ov[1],
                "state": "active", "tier": tier, "category": category,
                "app": app, "domain": "",
                "title": data.get("title", ""),
            })
    segments.sort(key=lambda s: s["start"])
    return segments


# ── §2 step 3 — browser override ──────────────────────────────────────────────

def _classify_web(app: str, domain: str, url: str) -> tuple[str, str]:
    from agent.tracking.categorizer import classify
    rule = classify(app, domain, url)
    if rule:
        return rule["tier"], rule["category"]
    return "distraction", "browsing"


def _apply_browser_override(segments: list[dict], web_events: list[dict], cfg: dict) -> list[dict]:
    """
    Where the focused app is a browser, replace "Google Chrome" with the
    domain-level web event for that interval.

    Internal Chrome pages (chrome://, chrome-extension://, about:) are classified
    as neutral/system — aw-watcher-web reports them via the tabs API and they are
    expected, not a sign of a dead extension.

    Portions of browser focus with no matching web event at all become
    `browser-unknown` (only genuine real-browsing gaps; flagged in §5).
    """
    from agent.tracking.categorizer import _extract_domain

    browser_names = set(cfg["browser_app_names"])
    neutral_schemes = tuple(cfg.get("neutral_url_schemes",
                                    ["chrome://", "chrome-extension://", "about:"]))
    min_dwell_sec = cfg.get("min_dwell_seconds", 3)

    web_spans: list[tuple] = []      # (start, end, w_app, domain, url) — real tab URLs
    internal_spans: list[tuple] = [] # (start, end) — chrome://, about: → neutral

    for ev in web_events:
        data = ev.get("data", {})
        url = data.get("url", "")
        if not url:
            continue
        start, end = _span(ev)
        if end <= start:
            continue
        if url.startswith(neutral_schemes):
            internal_spans.append((start, end))
            continue
        domain = _extract_domain(url)
        if not domain:
            continue
        web_spans.append((start, end, data.get("app", "Browser"), domain, url))

    web_spans.sort(key=lambda s: s[0])
    internal_spans.sort(key=lambda s: s[0])

    out = []
    for seg in segments:
        if seg["app"] not in browser_names:
            out.append(seg)
            continue

        s_start, s_end = seg["start"], seg["end"]
        title = seg.get("title", "")
        covered: list[tuple] = []

        for w_start, w_end, w_app, domain, url in web_spans:
            ov = _overlap(s_start, s_end, w_start, w_end)
            if not ov:
                continue
            tier, category = _classify_web(w_app, domain, url)
            out.append({
                "start": ov[0], "end": ov[1],
                "state": "active", "tier": tier, "category": category,
                "app": w_app, "domain": domain,
            })
            covered.append(ov)

        for i_start, i_end in internal_spans:
            ov = _overlap(s_start, s_end, i_start, i_end)
            if not ov:
                continue
            out.append({
                "start": ov[0], "end": ov[1],
                "state": "active", "tier": "neutral", "category": "system",
                "app": seg["app"], "domain": "",
            })
            covered.append(ov)

        for gap_start, gap_end in _gaps(s_start, s_end, covered):
            gap_sec = (gap_end - gap_start).total_seconds()
            if gap_sec < min_dwell_sec:
                # Population 1 — flicker: Chrome focused for a beat during an
                # app/tab switch. Absorb as neutral; do not count as browsing.
                out.append({
                    "start": gap_start, "end": gap_end,
                    "state": "active", "tier": "neutral", "category": "system",
                    "app": seg["app"], "domain": "",
                })
            else:
                # Population 2 — sustained gap: extension dropped a live tab.
                # Try the window title as a fallback classification.
                domain = _title_domain(title, cfg)
                if domain:
                    tier, category = _classify_web("Browser", domain, "")
                    out.append({
                        "start": gap_start, "end": gap_end,
                        "state": "active", "tier": tier, "category": category,
                        "app": "Browser", "domain": domain,
                        "label_source": "title",
                    })
                else:
                    # No confident title match — honestly unknown.
                    out.append({
                        "start": gap_start, "end": gap_end,
                        "state": "active", "tier": "distraction", "category": "browser-unknown",
                        "app": seg["app"], "domain": "",
                    })

    out.sort(key=lambda s: s["start"])
    return out


def _gaps(start: datetime, end: datetime, covered: list[tuple]) -> list[tuple]:
    """Portions of [start, end) not covered by any interval in `covered`."""
    covered = sorted(covered)
    gaps = []
    cursor = start
    for c_start, c_end in covered:
        if c_start > cursor:
            gaps.append((cursor, min(c_start, end)))
        cursor = max(cursor, c_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return [(s, e) for s, e in gaps if e > s]


# ── §3 — passive-engagement override ─────────────────────────────────────────

def _is_engaged(app: str, domain: str, tier: str, cfg: dict) -> bool:
    if app in cfg["engaged_apps"]:
        return True
    if domain and any(domain == d or domain.endswith(f".{d}") for d in cfg["engaged_domains"]):
        return True
    if domain and any(domain == d or domain.endswith(f".{d}") for d in cfg["engaged_video_domains"]):
        return tier == "deep"  # whitelisted-deep media only — never bare autoplay
    return False


def _engaged_spans_in(start: datetime, end: datetime, window_events, web_events, cfg) -> list[dict]:
    """Focused-context spans inside [start, end) that belong to the engaged set."""
    from agent.tracking.categorizer import _extract_domain

    spans = []
    for ev in window_events:
        data = ev.get("data", {})
        app = data.get("app", "")
        ev_start, ev_end = _span(ev)
        ov = _overlap(ev_start, ev_end, start, end)
        if not ov:
            continue
        tier, category = _classify_app(app)
        if _is_engaged(app, "", tier, cfg):
            spans.append({"start": ov[0], "end": ov[1], "tier": tier, "category": category,
                          "app": app, "domain": ""})

    for ev in web_events:
        data = ev.get("data", {})
        url = data.get("url", "")
        domain = _extract_domain(url)
        if not domain:
            continue
        ev_start, ev_end = _span(ev)
        ov = _overlap(ev_start, ev_end, start, end)
        if not ov:
            continue
        tier, category = _classify_web(data.get("app", "Browser"), domain, url)
        if _is_engaged("", domain, tier, cfg):
            spans.append({"start": ov[0], "end": ov[1], "tier": tier, "category": category,
                          "app": data.get("app", "Browser"), "domain": domain})

    spans.sort(key=lambda s: s["start"])
    return spans


def _apply_passive_override(afk_intervals: list[tuple], window_events, web_events, cfg) -> list[dict]:
    """
    Reclassify engaged portions of `afk` intervals back to active, capped at
    `override_cap_minutes` of *continuous* override per afk interval. Excess —
    and everything else (including non-whitelisted autoplay) — stays idle.
    """
    cap_sec = cfg["override_cap_minutes"] * 60
    out = []

    for iv_start, iv_end, _ in afk_intervals:
        spans = _engaged_spans_in(iv_start, iv_end, window_events, web_events, cfg)
        accepted: list[tuple] = []
        used_sec = 0.0

        for span in spans:
            if used_sec >= cap_sec:
                break
            s_start, s_end = span["start"], span["end"]
            dur = (s_end - s_start).total_seconds()
            remaining = cap_sec - used_sec
            if dur > remaining:
                s_end = s_start + timedelta(seconds=remaining)
                dur = remaining
            out.append({
                "start": s_start, "end": s_end, "state": "active",
                "tier": span["tier"], "category": span["category"],
                "app": span["app"], "domain": span["domain"],
            })
            accepted.append((s_start, s_end))
            used_sec += dur

        for gap_start, gap_end in _gaps(iv_start, iv_end, accepted):
            out.append({
                "start": gap_start, "end": gap_end, "state": "idle",
                "tier": _IDLE_TIER, "category": None, "app": "", "domain": "",
            })

    out.sort(key=lambda s: s["start"])
    return out


# ── Driver ────────────────────────────────────────────────────────────────────

def _infer_range(*event_lists) -> tuple[datetime | None, datetime | None]:
    starts, ends = [], []
    for events in event_lists:
        for ev in events:
            s, e = _span(ev)
            starts.append(s)
            ends.append(e)
    if not starts:
        return None, None
    return min(starts), max(ends)


def resolve_timeline(events: dict, config: dict | None = None,
                     range_start: datetime | None = None,
                     range_end: datetime | None = None) -> dict:
    """
    Run the full §2-3 merge pipeline.

    Returns:
      {
        "timeline": [{start, end, state, tier, category, app, domain}, ...],
        "active_minutes": float, "idle_minutes": float, "untracked_minutes": float,
      }
    `timeline` is sorted, gapless, and non-overlapping over [range_start, range_end).
    `state` is one of "active" | "idle" | "untracked"; only "active" entries carry
    a meaningful tier/category/app/domain.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    afk_events = events.get("afk", [])
    window_events = events.get("window", [])
    web_events = events.get("web", [])

    if range_start is None or range_end is None:
        inferred_start, inferred_end = _infer_range(afk_events, window_events, web_events)
        range_start = range_start or inferred_start
        range_end = range_end or inferred_end
    if range_start is None or range_end is None or range_end <= range_start:
        return {"timeline": [], "active_minutes": 0.0, "idle_minutes": 0.0, "untracked_minutes": 0.0}

    base = partition_afk(afk_events, range_start, range_end)
    not_afk = [iv for iv in base if iv[2] == "not-afk"]
    afk_iv  = [iv for iv in base if iv[2] == "afk"]
    untracked_iv = [iv for iv in base if iv[2] == "untracked"]

    active_segments = _active_app_segments(window_events, not_afk)
    active_segments = _apply_browser_override(active_segments, web_events, cfg)

    # Fill any not-afk gaps the window watcher didn't cover (rare) as active/unknown.
    for na_start, na_end, _ in not_afk:
        covered = [(s["start"], s["end"]) for s in active_segments
                   if _overlap(s["start"], s["end"], na_start, na_end)]
        for gap_start, gap_end in _gaps(na_start, na_end, covered):
            active_segments.append({
                "start": gap_start, "end": gap_end, "state": "active",
                "tier": "neutral", "category": "system", "app": "", "domain": "",
            })

    override_segments = _apply_passive_override(afk_iv, window_events, web_events, cfg)

    timeline = active_segments + override_segments + [
        {"start": s, "end": e, "state": "untracked", "tier": _UNTRACKED_TIER,
         "category": None, "app": "", "domain": ""}
        for s, e, _ in untracked_iv
    ]
    timeline.sort(key=lambda iv: iv["start"])

    active_sec = sum((iv["end"] - iv["start"]).total_seconds() for iv in timeline if iv["state"] == "active")
    idle_sec = sum((iv["end"] - iv["start"]).total_seconds() for iv in timeline if iv["state"] == "idle")
    untracked_sec = sum((iv["end"] - iv["start"]).total_seconds() for iv in timeline if iv["state"] == "untracked")

    return {
        "timeline": timeline,
        "active_minutes": round(active_sec / 60.0, 2),
        "idle_minutes": round(idle_sec / 60.0, 2),
        "untracked_minutes": round(untracked_sec / 60.0, 2),
    }


# ── §5 — health & coverage detection ─────────────────────────────────────────

# Browsers we don't run — if one of these is ever the focused, active app, the
# Chrome-only assumption behind the URL-coverage pipeline is broken for that time.
_NON_CHROME_BROWSERS = {"Safari", "Firefox", "Microsoft Edge", "Opera", "Brave Browser", "Arc"}


def detect_flags(result: dict, config: dict | None = None,
                 untracked_flag_threshold: float = 0.5) -> list[dict]:
    """
    Surface silent measurement failures (§5) from a resolved timeline:
      - chrome_unlabeled   — Chrome-focused active time with low URL coverage
                             (extension down / not loaded for some tabs)
      - non_chrome_browser — a browser outside `browser_app_names` was focused
      - high_untracked     — untracked time crosses the given threshold of the
                             measured range (not an error — system sleep is
                             expected — but worth surfacing so a half-tracked
                             day isn't read as a lazy one)
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    flags = []

    timeline = result.get("timeline", [])
    active = [iv for iv in timeline if iv["state"] == "active"]

    unknown_warn_min = cfg.get("unknown_warn_minutes", 5)

    # browser-unknown = sustained Chrome focus where neither the extension nor the
    # window title could identify the page. Warn when this residue is substantial.
    unknown_sec = sum(
        (iv["end"] - iv["start"]).total_seconds()
        for iv in active if iv.get("category") == "browser-unknown"
    )
    if unknown_sec / 60 >= unknown_warn_min:
        flags.append({
            "type": "chrome_unlabeled",
            "message": (f"Chrome extension missed ~{round(unknown_sec / 60, 1)}m of browsing "
                        "— some categories estimated from window titles."),
            "unlabeled_minutes": round(unknown_sec / 60, 1),
        })

    non_chrome_sec = sum((iv["end"] - iv["start"]).total_seconds()
                         for iv in active if iv["app"] in _NON_CHROME_BROWSERS)
    if non_chrome_sec > 0:
        flags.append({
            "type": "non_chrome_browser",
            "message": (f"{round(non_chrome_sec / 60, 1)}m focused in a non-Chrome browser — "
                        "that time has no URL-level labels (Chrome-only instrumentation)."),
            "minutes": round(non_chrome_sec / 60, 1),
        })

    elapsed_sec = sum((iv["end"] - iv["start"]).total_seconds() for iv in timeline)
    untracked_sec = result.get("untracked_minutes", 0) * 60
    if elapsed_sec > 0 and (untracked_sec / elapsed_sec) >= untracked_flag_threshold:
        flags.append({
            "type": "high_untracked",
            "message": (f"{round(untracked_sec / 60)}m untracked "
                        f"({untracked_sec / elapsed_sec:.0%} of the measured range) — "
                        "system asleep, watcher down, or a crash."),
            "fraction": round(untracked_sec / elapsed_sec, 3),
        })

    return flags
