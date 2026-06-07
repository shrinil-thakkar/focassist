"""
Tier-1 rule-based categorizer.
Rules are loaded from the backend (/rules) and cached locally.
Anything that doesn't match a rule goes into the ambiguous queue.
"""
import re
import logging
from urllib.parse import urlparse
from collections import defaultdict

log = logging.getLogger(__name__)

# --- Seed rules bundled with the agent (before first backend sync) ---
SEED_RULES: list[dict] = [
    # Productive
    {"match_type": "app", "match_value": "Xcode", "category": "dev", "productive": True},
    {"match_type": "app", "match_value": "Terminal", "category": "dev", "productive": True},
    {"match_type": "app", "match_value": "iTerm2", "category": "dev", "productive": True},
    {"match_type": "app", "match_value": "Visual Studio Code", "category": "dev", "productive": True},
    {"match_type": "app", "match_value": "PyCharm", "category": "dev", "productive": True},
    {"match_type": "domain", "match_value": "github.com", "category": "dev", "productive": True},
    {"match_type": "domain", "match_value": "stackoverflow.com", "category": "dev", "productive": True},
    {"match_type": "domain", "match_value": "docs.python.org", "category": "dev", "productive": True},
    {"match_type": "domain", "match_value": "notion.so", "category": "work", "productive": True},
    {"match_type": "domain", "match_value": "linear.app", "category": "work", "productive": True},
    {"match_type": "domain", "match_value": "figma.com", "category": "design", "productive": True},
    {"match_type": "app", "match_value": "Slack", "category": "comms", "productive": True},
    {"match_type": "domain", "match_value": "mail.google.com", "category": "comms", "productive": True},
    # Unproductive
    {"match_type": "domain", "match_value": "youtube.com", "category": "video", "productive": False},
    {"match_type": "domain", "match_value": "reddit.com", "category": "social", "productive": False},
    {"match_type": "domain", "match_value": "twitter.com", "category": "social", "productive": False},
    {"match_type": "domain", "match_value": "x.com", "category": "social", "productive": False},
    {"match_type": "domain", "match_value": "instagram.com", "category": "social", "productive": False},
    {"match_type": "domain", "match_value": "tiktok.com", "category": "social", "productive": False},
    {"match_type": "domain", "match_value": "netflix.com", "category": "video", "productive": False},
    {"match_type": "domain", "match_value": "twitch.tv", "category": "video", "productive": False},
    {"match_type": "domain", "match_value": "primevideo.com", "category": "video", "productive": False},
    {"match_type": "domain", "match_value": "disneyplus.com", "category": "video", "productive": False},
    {"match_type": "domain", "match_value": "hulu.com", "category": "video", "productive": False},
    {"match_type": "domain", "match_value": "hbomax.com", "category": "video", "productive": False},
    {"match_type": "domain", "match_value": "max.com", "category": "video", "productive": False},
    # Neutral (skip from ambiguous — treat as neutral)
    {"match_type": "app", "match_value": "Finder", "category": "system", "productive": True},
    {"match_type": "app", "match_value": "System Preferences", "category": "system", "productive": True},
    {"match_type": "app", "match_value": "System Settings", "category": "system", "productive": True},
]

_rules: list[dict] = []
_rules_loaded = False


def load_rules(backend_rules: list[dict] | None = None) -> None:
    global _rules, _rules_loaded
    combined = SEED_RULES.copy()
    if backend_rules:
        combined = backend_rules + combined  # backend rules take priority
    _rules = combined
    _rules_loaded = True


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = parsed.netloc or parsed.path
        return host.removeprefix("www.")
    except Exception:
        return url


def _match_rule(rule: dict, app: str, domain: str) -> bool:
    mt = rule["match_type"]
    mv = rule["match_value"]
    if mt == "domain":
        return domain == mv or domain.endswith(f".{mv}")
    if mt == "app":
        return app == mv
    if mt == "regex":
        return bool(re.search(mv, domain or app, re.IGNORECASE))
    return False


def classify(app: str, domain: str) -> dict | None:
    """Return matching rule dict or None if ambiguous."""
    if not _rules_loaded:
        load_rules()
    for rule in _rules:
        if _match_rule(rule, app, domain):
            return rule
    return None


def categorize_events(events: dict) -> tuple[list[dict], list[dict]]:
    """
    Roll up window + web events into aggregates and ambiguous items.
    Web events override window events for the same time slice when a URL is known.
    """
    if not _rules_loaded:
        load_rules()

    # Build a per-second map from web events (url -> domain)
    # AW events: {"timestamp": ..., "duration": float (seconds), "data": {...}}

    agg: dict[tuple, float] = defaultdict(float)   # (category, app, domain) -> minutes
    amb: dict[tuple, list] = defaultdict(list)      # (app, domain, title) -> [minutes]

    # Process window events as base
    for ev in events.get("window", []):
        data = ev.get("data", {})
        app = data.get("app", "")
        title = data.get("title", "")
        duration_min = ev.get("duration", 0) / 60.0
        if duration_min <= 0:
            continue
        domain = ""
        rule = classify(app, domain)
        if rule:
            key = (rule["category"], app, domain)
            agg[key] += duration_min
        else:
            amb[(app, domain, title)].append(duration_min)

    # Process web events — these are more specific; domain overrides app entry
    for ev in events.get("web", []):
        data = ev.get("data", {})
        url = data.get("url", "")
        title = data.get("title", "")
        app = data.get("app", "Browser")
        duration_min = ev.get("duration", 0) / 60.0
        if duration_min <= 0:
            continue
        domain = _extract_domain(url)
        rule = classify(app, domain)
        if rule:
            key = (rule["category"], app, domain)
            agg[key] += duration_min
        else:
            amb[(app, domain, title)].append(duration_min)

    aggregates = [
        {"category": k[0], "app": k[1], "domain": k[2], "minutes": round(v, 2)}
        for k, v in agg.items()
    ]
    ambiguous = [
        {"app": k[0], "domain": k[1], "title": k[2], "minutes": round(sum(v), 2)}
        for k, v in amb.items()
        if sum(v) > 0.5  # skip sub-30-second blips
    ]

    return aggregates, ambiguous
