"""Layered email classifier: deterministic rules first, LLM for the remainder.

Labels are stored locally only (emails_labeled.json) — nothing here writes
back to Gmail. Read-only downstream of agent/google/gmail_client.py.

Rules live in agent/labeling/rules.py (pure, network-free) and the LLM layer
lives in agent/labeling/llm.py (batched Gemini calls via Vertex AI) — this
module just wires the two together as the CLI entry point.

classify() is the per-email interface a future apply_labels_to_gmail.py can
reuse unchanged; label_batch() is the efficient bulk path this module's CLI
uses (batches the LLM layer 10-15 emails per call instead of one-by-one).
"""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from agent.labeling import cache
from agent.labeling.llm import (
    BATCH_SIZE,
    INTER_BATCH_DELAY_SECONDS,
    NEEDS_REVIEW_CONFIDENCE_THRESHOLD,
    call_batch_with_retry,
)
from agent.labeling.rules import Label, classify_rules, finalize, is_unresolved

# Same FOCASSIST_DIR convention as credentials/token/db (agent/google/auth.py).
_DIR = Path(os.environ.get("FOCASSIST_DIR", Path.home() / ".focassist"))
DEFAULT_EMAILS_PATH = str(_DIR / "emails_last_week.json")
DEFAULT_LABELED_PATH = str(_DIR / "emails_labeled.json")


def classify(email: dict) -> Label:
    """Classify a single email. Rules first, LLM fallback — the interface a
    future apply_labels_to_gmail.py can reuse unchanged."""
    direction = email.get("direction")
    rule_label = classify_rules(email)

    if not is_unresolved(rule_label, direction):
        return finalize(rule_label, direction)

    llm_results = call_batch_with_retry([email])
    parsed = llm_results.get(email["id"])
    if parsed is None:
        final = Label(action="fyi", category="personal", method="llm-fallback")
    else:
        category = rule_label.category if rule_label else parsed["category"]
        final = Label(
            action=parsed["action"], category=category, method="llm",
            confidence=parsed.get("confidence"), needs_review=parsed.get("needs_review", False),
        )
    return finalize(final, direction)


def label_batch(
    emails: list[dict],
    use_cache: bool = False,
    cache_path: str = cache.DEFAULT_CACHE_PATH,
) -> list[dict]:
    """Efficient bulk path: rules for everything, then one LLM call per
    BATCH_SIZE unresolved emails instead of one call per email. With
    use_cache=True, unchanged emails (same id + clean_body) reuse a prior
    run's LLM result instead of hitting the model again."""
    resolved: dict[str, Label] = {}
    pending: list[tuple[dict, str | None]] = []  # (email, rule_category_hint)

    cache_data = cache.load_cache(cache_path) if use_cache else {}
    cache_hits = 0

    for email in emails:
        direction = email.get("direction")
        rule_label = classify_rules(email)
        if not is_unresolved(rule_label, direction):
            resolved[email["id"]] = finalize(rule_label, direction)
            continue

        hint = rule_label.category if rule_label else None

        if use_cache:
            cached = cache_data.get(cache.cache_key(email))
            if cached is not None:
                cache_hits += 1
                category = hint if hint else cached["category"]
                final = Label(
                    action=cached["action"], category=category, method="llm",
                    confidence=cached.get("confidence"), needs_review=cached.get("needs_review", False),
                )
                resolved[email["id"]] = finalize(final, direction)
                continue

        pending.append((email, hint))

    for i in range(0, len(pending), BATCH_SIZE):
        if i > 0:
            time.sleep(INTER_BATCH_DELAY_SECONDS)
        chunk = pending[i:i + BATCH_SIZE]
        print(f"  labeling batch {i // BATCH_SIZE + 1} ({len(chunk)} emails)...", file=sys.stderr)
        llm_results = call_batch_with_retry([e for e, _ in chunk])
        for email, hint in chunk:
            parsed = llm_results.get(email["id"])
            if parsed is None:
                final = Label(action="fyi", category="personal", method="llm-fallback")
            else:
                category = hint if hint else parsed["category"]
                final = Label(
                    action=parsed["action"], category=category, method="llm",
                    confidence=parsed.get("confidence"), needs_review=parsed.get("needs_review", False),
                )
                if use_cache:
                    cache_data[cache.cache_key(email)] = parsed
            resolved[email["id"]] = finalize(final, email.get("direction"))

    if use_cache:
        cache.save_cache(cache_data, cache_path)
        if cache_hits:
            print(f"  cache: reused {cache_hits} previously-labeled email(s)", file=sys.stderr)

    return [{**email, "label": asdict(resolved[email["id"]])} for email in emails]


def print_summary(labeled: list[dict]) -> None:
    total = len(labeled)
    by_category: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_method: dict[str, int] = {}
    needs_review_count = 0

    for e in labeled:
        label = e["label"]
        by_category[label["category"]] = by_category.get(label["category"], 0) + 1
        action = label["action"] or "(none)"
        by_action[action] = by_action.get(action, 0) + 1
        by_method[label["method"]] = by_method.get(label["method"], 0) + 1
        if label.get("needs_review"):
            needs_review_count += 1

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

    print(f"\nNeeds review (confidence < {NEEDS_REVIEW_CONFIDENCE_THRESHOLD}): {needs_review_count}", file=sys.stderr)
    print(f"LLM fallback (parse failures): {by_method.get('llm-fallback', 0)}", file=sys.stderr)

    print("\nMost recent needs-reply:", file=sys.stderr)
    needs_reply = [e for e in labeled if e["label"]["action"] == "needs-reply"][:5]
    if not needs_reply:
        print("  (none)", file=sys.stderr)
    for e in needs_reply:
        print(f"  {e.get('from')} — {e.get('subject')}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", default=DEFAULT_EMAILS_PATH)
    parser.add_argument("--out", dest="out_path", default=DEFAULT_LABELED_PATH)
    parser.add_argument("--cache", action="store_true",
                         help="Reuse cached LLM labels for unchanged emails across runs")
    parser.add_argument("--cache-file", default=cache.DEFAULT_CACHE_PATH)
    args = parser.parse_args()

    with open(args.in_path) as f:
        emails = json.load(f)

    labeled = label_batch(emails, use_cache=args.cache, cache_path=args.cache_file)

    Path(args.out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_path, "w") as f:
        json.dump(labeled, f, indent=2)

    print_summary(labeled)
    print(f"\nWrote {len(labeled)} labeled emails to {args.out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
