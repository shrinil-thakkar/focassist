"""Layer 0 — sanitize email bodies before rules or the LLM ever see them.

Pure function, no network. Produces `clean_body` from a raw plain-text or
HTML body: strips script/style, strips elements hidden via CSS tricks
(display:none, visibility:hidden, opacity:0, font-size:0, matching
text/background color), strips zero-width characters, and normalizes
Unicode to NFC. This is the boundary that stops a hidden instruction
embedded in an email from ever reaching the model.

Safe to run on plain text too — none of the HTML-targeted patterns match,
so it degrades to just the zero-width-strip + NFC-normalize passes.
"""

import re
import unicodedata

# Zero-width space, ZWNJ, ZWJ, BOM/zero-width-no-break-space.
ZERO_WIDTH_CHARS = ("​", "‌", "‍", "﻿")

_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")

# Tags carrying an inline `style="..."` attribute, captured so we can inspect
# the style content before deciding whether to drop the whole element.
_STYLED_TAG_RE = re.compile(
    r"(?is)<([a-zA-Z][\w-]*)\b[^>]*\bstyle\s*=\s*(['\"])(.*?)\2[^>]*>(.*?)</\1>"
)

_HIDDEN_STYLE_PATTERNS = (
    re.compile(r"(?i)display\s*:\s*none"),
    re.compile(r"(?i)visibility\s*:\s*hidden"),
    re.compile(r"(?i)opacity\s*:\s*0(?:\.0+)?\b"),
    re.compile(r"(?i)font-size\s*:\s*0(?:px)?\b"),
    # off-screen positioning: absolute/fixed + a large negative offset
    re.compile(r"(?i)position\s*:\s*(?:absolute|fixed).{0,80}(?:left|top)\s*:\s*-\d{3,}px"),
)

_COLOR_RE = re.compile(r"(?i)(?<!background-)\bcolor\s*:\s*([^;]+)")
_BG_COLOR_RE = re.compile(r"(?i)background(?:-color)?\s*:\s*([^;]+)")


def _is_hidden_style(style: str) -> bool:
    if any(p.search(style) for p in _HIDDEN_STYLE_PATTERNS):
        return True
    color = _COLOR_RE.search(style)
    bg = _BG_COLOR_RE.search(style)
    if color and bg and color.group(1).strip().lower() == bg.group(1).strip().lower():
        return True  # text color matches background — invisible to a human reader
    return False


def _strip_hidden_elements(html: str) -> str:
    """Drop entire elements whose inline style hides them from view."""
    def _replace(m: re.Match) -> str:
        style = m.group(3)
        return "" if _is_hidden_style(style) else m.group(0)

    # Run repeatedly — nested hidden elements and overlapping matches on a
    # single regex pass aren't guaranteed to all be caught in one go.
    prev = None
    out = html
    while prev != out:
        prev = out
        out = _STYLED_TAG_RE.sub(_replace, out)
    return out


def _strip_tags(html: str) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def strip_zero_width(text: str) -> str:
    for ch in ZERO_WIDTH_CHARS:
        text = text.replace(ch, "")
    return text


def sanitize_body(raw: str) -> str:
    """Turn a raw plain-text or HTML body into safe-for-LLM `clean_body`."""
    if not raw:
        return ""
    text = _SCRIPT_STYLE_RE.sub("", raw)
    text = _strip_hidden_elements(text)
    text = _strip_tags(text)
    text = strip_zero_width(text)
    text = unicodedata.normalize("NFC", text)
    return text.strip()
