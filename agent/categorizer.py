"""
Tier-1 rule-based categorizer.

Tiers: deep | supporting | neutral | distraction
Match types (specificity order): url_match > domain/regex > app
Unknown → distraction (not ambiguous; ambiguous queue is for whitelist suggestions only).
"""
import re
import logging
from urllib.parse import urlparse
from collections import defaultdict

log = logging.getLogger(__name__)

BROWSER_APPS = {
    "Google Chrome", "Chrome", "Chromium",
    "Firefox", "Safari", "Microsoft Edge",
    "Opera", "Brave Browser", "Arc",
    # Chrome PWAs — ActivityWatch reports these as separate app names but they
    # are browser windows and must be overridden by web events, not treated as
    # native apps (otherwise the domain entry and the PWA entry both appear).
    "Telegram Web", "WhatsApp Web",
}

# Seed rules — backend rules loaded at runtime take priority.
# Rule shape: {match_type, match_value, tier, category, productive}
#   match_type : domain | app | url_match | regex
#   tier       : deep | supporting | neutral | distraction
#   category   : subcategory for display (coding, comms, video, …)
SEED_RULES: list[dict] = [
    # ── Deep work — coding apps ───────────────────────────────────────────────
    {"match_type": "app", "match_value": "Terminal",           "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "iTerm2",             "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "Xcode",              "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "Visual Studio Code", "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "Cursor",             "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "PyCharm",            "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "IntelliJ IDEA",      "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "WebStorm",           "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "GoLand",             "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "Sublime Text",       "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "Vim",                "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "Neovim",             "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "Postman",            "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "Insomnia",           "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "TablePlus",          "tier": "deep", "category": "coding"},
    {"match_type": "app", "match_value": "Docker Desktop",     "tier": "deep", "category": "coding"},
    # ── Deep work — coding domains ────────────────────────────────────────────
    {"match_type": "domain",    "match_value": "github.com",              "tier": "deep", "category": "coding"},
    {"match_type": "domain",    "match_value": "gitlab.com",              "tier": "deep", "category": "coding"},
    {"match_type": "domain",    "match_value": "stackoverflow.com",       "tier": "deep", "category": "coding"},
    {"match_type": "domain",    "match_value": "docs.python.org",         "tier": "deep", "category": "docs"},
    {"match_type": "domain",    "match_value": "developer.mozilla.org",   "tier": "deep", "category": "docs"},
    {"match_type": "domain",    "match_value": "readthedocs.io",          "tier": "deep", "category": "docs"},
    {"match_type": "domain",    "match_value": "pypi.org",                "tier": "deep", "category": "coding"},
    {"match_type": "domain",    "match_value": "npmjs.com",               "tier": "deep", "category": "coding"},
    {"match_type": "domain",    "match_value": "figma.com",               "tier": "deep", "category": "design"},
    {"match_type": "domain",    "match_value": "localhost",               "tier": "deep", "category": "coding"},
    {"match_type": "domain",    "match_value": "127.0.0.1",               "tier": "deep", "category": "coding"},
    {"match_type": "regex",     "match_value": r"^docs\.",                "tier": "deep", "category": "docs"},
    # ── Deep work — AI coding assistants ─────────────────────────────────────
    {"match_type": "domain",    "match_value": "claude.ai",               "tier": "deep", "category": "ai"},
    {"match_type": "domain",    "match_value": "chatgpt.com",             "tier": "deep", "category": "ai"},
    {"match_type": "domain",    "match_value": "gemini.google.com",       "tier": "deep", "category": "ai"},
    # ── Deep work — whitelist overrides (beat distraction rules below) ────────
    {"match_type": "url_match", "match_value": "reddit.com/r/programming",    "tier": "deep", "category": "coding"},
    {"match_type": "url_match", "match_value": "reddit.com/r/python",         "tier": "deep", "category": "coding"},
    {"match_type": "url_match", "match_value": "reddit.com/r/rust",           "tier": "deep", "category": "coding"},
    {"match_type": "url_match", "match_value": "reddit.com/r/golang",         "tier": "deep", "category": "coding"},
    {"match_type": "url_match", "match_value": "reddit.com/r/machinelearning","tier": "deep", "category": "coding"},
    {"match_type": "url_match", "match_value": "reddit.com/r/cscareerquestions","tier": "deep", "category": "coding"},
    {"match_type": "domain",    "match_value": "news.ycombinator.com",        "tier": "deep", "category": "coding"},
    # ── Supporting ────────────────────────────────────────────────────────────
    {"match_type": "app",    "match_value": "Slack",             "tier": "supporting", "category": "comms"},
    {"match_type": "app",    "match_value": "Zoom",              "tier": "supporting", "category": "meetings"},
    {"match_type": "app",    "match_value": "Microsoft Teams",   "tier": "supporting", "category": "meetings"},
    {"match_type": "app",    "match_value": "Calendar",          "tier": "supporting", "category": "planning"},
    {"match_type": "app",    "match_value": "Fantastical",        "tier": "supporting", "category": "planning"},
    {"match_type": "domain", "match_value": "mail.google.com",   "tier": "supporting", "category": "comms"},
    {"match_type": "domain", "match_value": "outlook.com",       "tier": "supporting", "category": "comms"},
    {"match_type": "domain", "match_value": "linear.app",        "tier": "supporting", "category": "planning"},
    {"match_type": "domain", "match_value": "notion.so",         "tier": "supporting", "category": "planning"},
    {"match_type": "domain", "match_value": "atlassian.net",     "tier": "supporting", "category": "planning"},
    {"match_type": "domain", "match_value": "asana.com",         "tier": "supporting", "category": "planning"},
    {"match_type": "domain", "match_value": "trello.com",        "tier": "supporting", "category": "planning"},
    {"match_type": "domain", "match_value": "calendar.google.com","tier": "supporting", "category": "planning"},
    {"match_type": "domain", "match_value": "meet.google.com",   "tier": "supporting", "category": "meetings"},
    # ── Neutral ───────────────────────────────────────────────────────────────
    {"match_type": "app", "match_value": "Finder",             "tier": "neutral", "category": "system"},
    {"match_type": "app", "match_value": "System Preferences", "tier": "neutral", "category": "system"},
    {"match_type": "app", "match_value": "System Settings",    "tier": "neutral", "category": "system"},
    {"match_type": "app", "match_value": "Activity Monitor",   "tier": "neutral", "category": "system"},
    {"match_type": "app", "match_value": "loginwindow",        "tier": "neutral", "category": "system"},
    {"match_type": "app", "match_value": "Telegram",           "tier": "supporting", "category": "comms"},
    {"match_type": "app", "match_value": "Telegram Web",       "tier": "supporting", "category": "comms"},
    {"match_type": "domain", "match_value": "web.telegram.org","tier": "supporting", "category": "comms"},
    # ── Distraction — video ───────────────────────────────────────────────────
    {"match_type": "domain", "match_value": "youtube.com",     "tier": "distraction", "category": "video"},
    {"match_type": "domain", "match_value": "netflix.com",     "tier": "distraction", "category": "video"},
    {"match_type": "domain", "match_value": "primevideo.com",  "tier": "distraction", "category": "video"},
    {"match_type": "domain", "match_value": "twitch.tv",       "tier": "distraction", "category": "video"},
    {"match_type": "domain", "match_value": "disneyplus.com",  "tier": "distraction", "category": "video"},
    {"match_type": "domain", "match_value": "hulu.com",        "tier": "distraction", "category": "video"},
    {"match_type": "domain", "match_value": "max.com",         "tier": "distraction", "category": "video"},
    {"match_type": "domain", "match_value": "hbomax.com",      "tier": "distraction", "category": "video"},
    {"match_type": "domain", "match_value": "hotstar.com",     "tier": "distraction", "category": "video"},
    # ── Distraction — social ──────────────────────────────────────────────────
    {"match_type": "domain", "match_value": "reddit.com",      "tier": "distraction", "category": "social"},
    {"match_type": "domain", "match_value": "x.com",           "tier": "distraction", "category": "social"},
    {"match_type": "domain", "match_value": "twitter.com",     "tier": "distraction", "category": "social"},
    {"match_type": "domain", "match_value": "instagram.com",   "tier": "distraction", "category": "social"},
    {"match_type": "domain", "match_value": "tiktok.com",      "tier": "distraction", "category": "social"},
    {"match_type": "domain", "match_value": "facebook.com",    "tier": "distraction", "category": "social"},
    {"match_type": "domain", "match_value": "linkedin.com",    "tier": "distraction", "category": "social"},
]

_rules: list[dict] = []
_rules_loaded = False


def _enrich(rule: dict) -> dict:
    """Add productive flag derived from tier."""
    r = dict(rule)
    r.setdefault("productive", r.get("tier") in ("deep", "supporting", "neutral"))
    return r


def load_rules(backend_rules: list[dict] | None = None) -> None:
    global _rules, _rules_loaded
    seed = [_enrich(r) for r in SEED_RULES]
    backend = [_enrich(r) for r in (backend_rules or [])]
    # Backend rules take priority; seed rules are the fallback.
    _rules = backend + seed
    _rules_loaded = True


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = parsed.netloc or parsed.path.split("/")[0]
        return host.removeprefix("www.").split(":")[0]
    except Exception:
        return ""


def _match(rule: dict, app: str, domain: str, url: str) -> bool:
    mt, mv = rule["match_type"], rule["match_value"]
    if mt == "url_match":
        return mv in url
    if mt == "domain":
        return domain == mv or domain.endswith(f".{mv}")
    if mt == "app":
        return app == mv
    if mt == "regex":
        return bool(re.search(mv, domain or app, re.IGNORECASE))
    return False


def classify(app: str, domain: str, url: str = "") -> dict | None:
    """Return the best-matching rule or None (→ caller treats as distraction)."""
    if not _rules_loaded:
        load_rules()
    # Specificity: url_match first, then domain/regex, then app.
    for mt_group in (("url_match",), ("domain", "regex"), ("app",)):
        for rule in _rules:
            if rule["match_type"] in mt_group and _match(rule, app, domain, url):
                return rule
    return None


def categorize_events(events: dict) -> tuple[list[dict], list[dict]]:
    """
    Returns (aggregates, ambiguous).
    aggregates: [{tier, category, app, domain, minutes}]
    ambiguous:  [{app, domain, title, minutes}] — unmatched items queued for
                whitelist suggestions (M3); they already count as distraction.
    Neutral events are excluded from aggregates (excluded from all ratios).
    """
    if not _rules_loaded:
        load_rules()

    agg: dict[tuple, float] = defaultdict(float)
    amb: dict[tuple, list]  = defaultdict(list)

    # Non-browser window events
    for ev in events.get("window", []):
        data = ev.get("data", {})
        app   = data.get("app", "")
        title = data.get("title", "")
        dur   = ev.get("duration", 0) / 60.0
        if dur <= 0 or app in BROWSER_APPS:
            continue
        rule = classify(app, "", "")
        tier = rule["tier"] if rule else "distraction"
        cat  = rule["category"] if rule else "other"
        if tier == "neutral":
            continue
        agg[(tier, cat, app, "")] += dur
        if not rule:
            amb[(app, "", title)].append(dur)

    # Browser web events (Chrome extension)
    for ev in events.get("web", []):
        data  = ev.get("data", {})
        url   = data.get("url", "")
        title = data.get("title", "")
        app   = data.get("app", "Browser")
        dur   = ev.get("duration", 0) / 60.0
        if dur <= 0:
            continue
        domain = _extract_domain(url)
        if not domain or domain.startswith("chrome") or domain.startswith("about"):
            continue
        rule = classify(app, domain, url)
        tier = rule["tier"] if rule else "distraction"
        cat  = rule["category"] if rule else "browsing"
        if tier == "neutral":
            continue
        agg[(tier, cat, app, domain)] += dur
        if not rule:
            amb[(app, domain, title)].append(dur)

    aggregates = [
        {"tier": k[0], "category": k[1], "app": k[2], "domain": k[3], "minutes": round(v, 2)}
        for k, v in agg.items()
    ]
    ambiguous = [
        {"app": k[0], "domain": k[1], "title": k[2], "minutes": round(sum(v), 2)}
        for k, v in amb.items()
        if sum(v) > 0.5
    ]
    return aggregates, ambiguous
