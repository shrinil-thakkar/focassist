"""Layered email classifier: deterministic rules first, LLM for the remainder.

Labels are stored locally only (emails_labeled.json) — nothing here writes
back to Gmail. Read-only downstream of gmail_tool.py.

classify() is the per-email interface a future apply_labels_to_gmail.py can
reuse unchanged; label_batch() is the efficient bulk path this module's CLI
uses (batches the LLM layer 10-15 emails per call instead of one-by-one).
"""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml
from google import genai

MODEL = "gemini-2.5-flash-lite"
BATCH_SIZE = 12
BODY_CHARS_TO_LLM = 500

# Billed through GCP (Vertex AI) — same project/region conventions as google-genai.
GENAI_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "muti-agent-testing")
GENAI_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

ACTIONS = {"needs-reply", "action-needed", "fyi", "ignore"}
CATEGORIES = {"work", "personal", "finance", "fitness", "newsletter", "receipt", "promo"}

RECEIPT_KEYWORDS = ("invoice", "receipt", "payment", "order", "confirmation", "otp")
NOREPLY_PATTERNS = ("no-reply@", "noreply@", "donotreply")

_RULES_FILE = Path(__file__).parent / "label_rules.yaml"
_config = yaml.safe_load(_RULES_FILE.read_text()) or {}
FINANCE_DOMAINS = set(_config.get("finance_domains", []))
FITNESS_DOMAINS = set(_config.get("fitness_domains", []))
WORK_DOMAINS = set(_config.get("work_domains", []))


@dataclass
class Label:
    action: str | None
    category: str
    method: str  # "rule" | "llm" | "llm-fallback"
    rule: str | None = None
    confidence: float | None = None


# --- deterministic rule layer (pure, no network) -----------------------------
def _extract_domain(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender or "")
    return m.group(1).lower() if m else ""


def _is_receipt_sender(sender: str) -> bool:
    sender = sender.lower()
    return any(p in sender for p in NOREPLY_PATTERNS)


def _has_receipt_keywords(subject: str, body: str) -> bool:
    text = f"{subject} {body}".lower()
    return any(k in text for k in RECEIPT_KEYWORDS)


def classify_rules(email: dict) -> Label | None:
    """First-match-wins deterministic rules. Returns None if nothing matched."""
    labels = email.get("gmail_labels") or []
    sender = (email.get("from") or "").lower()
    subject = (email.get("subject") or "").lower()
    body = (email.get("body") or "").lower()

    if email.get("has_list_unsubscribe") and "CATEGORY_PERSONAL" not in labels:
        return Label(action="fyi", category="newsletter", method="rule", rule="list_unsubscribe")

    if "CATEGORY_PROMOTIONS" in labels:
        return Label(action="ignore", category="promo", method="rule", rule="category_promotions")

    if _is_receipt_sender(sender) and _has_receipt_keywords(subject, body):
        return Label(action="fyi", category="receipt", method="rule", rule="receipt_pattern")

    domain = _extract_domain(sender)
    if domain in FINANCE_DOMAINS:
        # Category is settled; action is left for the LLM (finance mail can be urgent).
        return Label(action=None, category="finance", method="rule", rule="finance_domain")

    if domain in FITNESS_DOMAINS:
        return Label(action="fyi", category="fitness", method="rule", rule="fitness_domain")

    if domain in WORK_DOMAINS:
        return Label(action=None, category="work", method="rule", rule="work_domain")

    return None


def _is_unresolved(label: Label | None, direction: str) -> bool:
    if label is None:
        return True
    # Sent emails always get action=None regardless of source, so a rule that
    # left action unset is still "resolved" for a sent email.
    return direction == "received" and label.action is None


def _finalize(label: Label, direction: str) -> Label:
    if direction == "sent":
        label.action = None
    return label


# --- LLM layer (batched) ------------------------------------------------------
def _client() -> genai.Client:
    return genai.Client(vertexai=True, project=GENAI_PROJECT_ID, location=GENAI_LOCATION)


def _build_prompt(batch: list[dict]) -> str:
    lines = [
        "Classify each email below along two axes.",
        "",
        "Axis 1 - action (received emails only; sent emails must get action: null):",
        "- needs-reply: expects a response from the user",
        "- action-needed: requires a non-reply task (pay, upload, review, RSVP)",
        "- fyi: worth knowing, nothing to do",
        "- ignore: noise",
        "",
        "Axis 2 - category (all emails):",
        "- work | personal | finance | fitness | newsletter | receipt | promo",
        "",
        "Return ONLY a JSON array, no prose and no code fences, one object per "
        'email in the same order: [{"id": "...", "category": "...", '
        '"action": "..." or null, "confidence": 0.0-1.0}, ...]',
        "",
        "Emails:",
    ]
    for i, e in enumerate(batch, 1):
        body = (e.get("body") or "")[:BODY_CHARS_TO_LLM]
        lines += [
            f"{i}. id: {e['id']}",
            f"   direction: {e.get('direction')}",
            f"   from: {e.get('from')}",
            f"   subject: {e.get('subject')}",
            f"   body: {body}",
        ]
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"```$", "", text)
    return text.strip()


def _call_llm_batch(batch: list[dict]) -> dict:
    """Returns {id: {"category":..., "action":..., "confidence":...}} for
    whatever the model returned validly. Missing/invalid entries are simply
    absent from the result — callers treat those as llm-fallback."""
    if not batch:
        return {}
    client = _client()
    try:
        response = client.models.generate_content(model=MODEL, contents=_build_prompt(batch))
        parsed = json.loads(_strip_code_fence(response.text or ""))
    except Exception as e:
        print(f"  LLM batch call failed: {e}", file=sys.stderr)
        return {}

    results = {}
    if not isinstance(parsed, list):
        return {}
    for item in parsed:
        if not isinstance(item, dict) or "id" not in item:
            continue
        category = item.get("category")
        action = item.get("action")
        if category not in CATEGORIES:
            continue
        if action is not None and action not in ACTIONS:
            continue
        results[item["id"]] = {
            "category": category,
            "action": action,
            "confidence": item.get("confidence"),
        }
    return results


# --- public interfaces --------------------------------------------------------
def classify(email: dict) -> Label:
    """Classify a single email. Rules first, LLM fallback — the interface a
    future apply_labels_to_gmail.py can reuse unchanged."""
    direction = email.get("direction")
    rule_label = classify_rules(email)

    if not _is_unresolved(rule_label, direction):
        return _finalize(rule_label, direction)

    llm_results = _call_llm_batch([email])
    parsed = llm_results.get(email["id"])
    if parsed is None:
        final = Label(action="fyi", category="personal", method="llm-fallback")
    else:
        category = rule_label.category if rule_label else parsed["category"]
        final = Label(
            action=parsed["action"], category=category, method="llm",
            confidence=parsed.get("confidence"),
        )
    return _finalize(final, direction)


def label_batch(emails: list[dict]) -> list[dict]:
    """Efficient bulk path: rules for everything, then one LLM call per
    BATCH_SIZE unresolved emails instead of one call per email."""
    resolved: dict[str, Label] = {}
    pending: list[tuple[dict, str | None]] = []  # (email, rule_category_hint)

    for email in emails:
        direction = email.get("direction")
        rule_label = classify_rules(email)
        if not _is_unresolved(rule_label, direction):
            resolved[email["id"]] = _finalize(rule_label, direction)
        else:
            hint = rule_label.category if rule_label else None
            pending.append((email, hint))

    for i in range(0, len(pending), BATCH_SIZE):
        chunk = pending[i:i + BATCH_SIZE]
        print(f"  labeling batch {i // BATCH_SIZE + 1} ({len(chunk)} emails)...", file=sys.stderr)
        llm_results = _call_llm_batch([e for e, _ in chunk])
        for email, hint in chunk:
            parsed = llm_results.get(email["id"])
            if parsed is None:
                final = Label(action="fyi", category="personal", method="llm-fallback")
            else:
                category = hint if hint else parsed["category"]
                final = Label(
                    action=parsed["action"], category=category, method="llm",
                    confidence=parsed.get("confidence"),
                )
            resolved[email["id"]] = _finalize(final, email.get("direction"))

    return [{**email, "label": asdict(resolved[email["id"]])} for email in emails]


# --- summary output ------------------------------------------------------------
def print_summary(labeled: list[dict]) -> None:
    total = len(labeled)
    by_category: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_method: dict[str, int] = {}

    for e in labeled:
        label = e["label"]
        by_category[label["category"]] = by_category.get(label["category"], 0) + 1
        action = label["action"] or "(none)"
        by_action[action] = by_action.get(action, 0) + 1
        by_method[label["method"]] = by_method.get(label["method"], 0) + 1

    print(f"\nLabeled {total} emails", file=sys.stderr)
    print("By category:", file=sys.stderr)
    for k, v in sorted(by_category.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}", file=sys.stderr)
    print("By action:", file=sys.stderr)
    for k, v in sorted(by_action.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}", file=sys.stderr)
    print("By method (rule vs LLM):", file=sys.stderr)
    for k, v in sorted(by_method.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}", file=sys.stderr)

    print("\nMost recent needs-reply:", file=sys.stderr)
    needs_reply = [e for e in labeled if e["label"]["action"] == "needs-reply"][:5]
    if not needs_reply:
        print("  (none)", file=sys.stderr)
    for e in needs_reply:
        print(f"  {e.get('from')} — {e.get('subject')}", file=sys.stderr)


# --- CLI -----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", default="emails_last_week.json")
    parser.add_argument("--out", dest="out_path", default="emails_labeled.json")
    args = parser.parse_args()

    with open(args.in_path) as f:
        emails = json.load(f)

    labeled = label_batch(emails)

    with open(args.out_path, "w") as f:
        json.dump(labeled, f, indent=2)

    print_summary(labeled)
    print(f"\nWrote {len(labeled)} labeled emails to {args.out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
