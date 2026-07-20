"""
Parse free-text daily plans into structured time_block rows.

Accepts lines like:
  9-11 deep work (coding)
  11-12 email + slack
  12-13 lunch
  14-16 focus [youtube.com, reddit.com]
  14-16 focus block
  16-17 review

Rules:
  - Time range must start the line: HH, HH:MM, HHam/pm variants, dash separator
  - "focus" or "deep work" in the label → kind=focus
  - lunch/break/rest/leisure/nap → kind=unproductive
  - everything else → kind=productive
  - [domain1, domain2] at end of line → block_domains for focus blocks
  - focus blocks with no explicit domains get DEFAULT_FOCUS_DOMAINS
"""
import re
import json

DEFAULT_FOCUS_DOMAINS = [
    "youtube.com", "reddit.com", "twitter.com", "x.com",
    "instagram.com", "primevideo.com",
]

_FOCUS_KEYWORDS    = {"focus", "deep work", "deep"}
_UNPRODUCT_KEYWORDS = {"lunch", "break", "rest", "nap", "leisure", "relax", "sleep"}

_TIME_RE = re.compile(
    r"^(\d{1,2}(?::\d{2})?(?:am|pm)?)\s*[-–]\s*(\d{1,2}(?::\d{2})?(?:am|pm)?)\s+",
    re.IGNORECASE,
)
_DOMAIN_LIST_RE = re.compile(r"\[([^\]]+)\]")


def parse_plan(raw: str, date: str) -> list[dict]:
    """Return a list of time_block dicts parsed from raw plan text."""
    blocks = []
    for line in raw.strip().splitlines():
        line = line.strip().lstrip("•-* ")
        if not line:
            continue
        block = _parse_line(line, date)
        if block:
            blocks.append(block)
    return blocks


def _parse_line(line: str, date: str) -> dict | None:
    m = _TIME_RE.match(line)
    if not m:
        return None

    start = _parse_time(m.group(1))
    end   = _parse_time(m.group(2))
    if not start or not end:
        return None

    rest = line[m.end():].strip()

    # Pull out explicit domain list
    custom_domains = None
    dm = _DOMAIN_LIST_RE.search(rest)
    if dm:
        custom_domains = [d.strip() for d in dm.group(1).split(",") if d.strip()]
        rest = (rest[:dm.start()] + rest[dm.end():]).strip()

    label = rest.strip()
    label_lower = label.lower()

    if any(kw in label_lower for kw in _FOCUS_KEYWORDS):
        kind = "focus"
    elif any(kw in label_lower for kw in _UNPRODUCT_KEYWORDS):
        kind = "unproductive"
    else:
        kind = "productive"

    if kind == "focus":
        domains = custom_domains if custom_domains is not None else DEFAULT_FOCUS_DOMAINS
    else:
        domains = []

    return {
        "date": date,
        "start": start,
        "end": end,
        "label": label or "block",
        "kind": kind,
        "block_domains": json.dumps(domains),
    }


def _parse_time(s: str) -> str | None:
    s = s.strip().lower()
    pm = s.endswith("pm")
    am = s.endswith("am")
    s = s.replace("am", "").replace("pm", "").strip()

    if ":" in s:
        parts = s.split(":")
        h, mn = int(parts[0]), int(parts[1])
    else:
        h, mn = int(s), 0

    if pm and h != 12:
        h += 12
    if am and h == 12:
        h = 0

    if 0 <= h <= 23 and 0 <= mn <= 59:
        return f"{h:02d}:{mn:02d}"
    return None


def format_blocks_confirmation(blocks: list[dict]) -> str:
    if not blocks:
        return "Couldn't parse any time blocks — plan saved as text."

    KIND_ICON = {"focus": "🎯", "productive": "💼", "unproductive": "😴"}
    lines = [f"✅ *{len(blocks)} block{'s' if len(blocks) != 1 else ''} scheduled:*\n"]
    for b in blocks:
        icon = KIND_ICON.get(b["kind"], "📌")
        lines.append(f"{icon} `{b['start']}–{b['end']}` {b['label']}")
        if b["kind"] == "focus":
            domains = json.loads(b["block_domains"])
            if domains:
                lines.append(f"   _Blocking: {', '.join(domains)}_")
    return "\n".join(lines)
