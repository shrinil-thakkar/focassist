"""Deterministic email-labeling rules — pure functions, no network.

First-match-wins. Sender-identity checks (finance, fitness, work) run BEFORE
the generic newsletter/promo/receipt heuristics — a lot of legitimate
finance and transactional mail carries a List-Unsubscribe header, so running
the newsletter check first would file a bank alert as newsletter/fyi and it
would never reach the finance rule. Sender-domain lists live in rules.yaml.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ACTIONS = {"needs-reply", "action-needed", "fyi", "ignore"}
CATEGORIES = {"work", "personal", "finance", "fitness", "newsletter", "receipt", "promo"}

RECEIPT_KEYWORDS = ("invoice", "receipt", "payment", "order", "confirmation", "otp")
NOREPLY_PATTERNS = ("no-reply@", "noreply@", "donotreply")

_RULES_FILE = Path(__file__).parent / "rules.yaml"
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
    needs_review: bool = False


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
    """First-match-wins deterministic rules. Returns None if nothing matched.

    Reads clean_body (falling back to body for older/cached fetches without
    it) so a hidden-text trick can't influence rule matching either.
    """
    labels = email.get("gmail_labels") or []
    sender = (email.get("from") or "").lower()
    subject = (email.get("subject") or "").lower()
    body = (email.get("clean_body") or email.get("body") or "").lower()
    domain = _extract_domain(sender)

    # 1-3: sender-identity checks, before the generic heuristics below.
    if domain in FINANCE_DOMAINS:
        # Category is settled; action is left for the LLM (finance mail can be urgent).
        return Label(action=None, category="finance", method="rule", rule="finance_domain")

    if domain in FITNESS_DOMAINS:
        return Label(action="fyi", category="fitness", method="rule", rule="fitness_domain")

    if domain in WORK_DOMAINS:
        return Label(action=None, category="work", method="rule", rule="work_domain")

    # 4-6: generic heuristics — unreachable for senders already matched above,
    # since every branch above returns immediately.
    if email.get("has_list_unsubscribe") and "CATEGORY_PERSONAL" not in labels:
        return Label(action="fyi", category="newsletter", method="rule", rule="list_unsubscribe")

    if "CATEGORY_PROMOTIONS" in labels:
        return Label(action="ignore", category="promo", method="rule", rule="category_promotions")

    if _is_receipt_sender(sender) and _has_receipt_keywords(subject, body):
        return Label(action="fyi", category="receipt", method="rule", rule="receipt_pattern")

    return None


def is_unresolved(label: Label | None, direction: str) -> bool:
    if label is None:
        return True
    # Sent emails always get action=None regardless of source, so a rule that
    # left action unset is still "resolved" for a sent email.
    return direction == "received" and label.action is None


def finalize(label: Label, direction: str) -> Label:
    if direction == "sent":
        label.action = None
    return label
