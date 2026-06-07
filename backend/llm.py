"""
LLM layer — Gemini free tier (v2, deferred).
In v1 this module is a stub; all categorization is rule-based (Tier 1).
"""


def classify_ambiguous(items: list[dict]) -> list[dict]:
    """
    Classify ambiguous items via Gemini.
    Returns the same list with 'category' and 'productive' filled in.
    v1 stub — returns items unchanged.
    """
    return items
