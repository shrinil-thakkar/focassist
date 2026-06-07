"""
Sync client: pushes aggregates to the backend, polls for directives and rules.
All communication is outbound from the Mac (poll + push over HTTPS).
"""
import os
import json
import logging
from datetime import date
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin

log = logging.getLogger(__name__)

BACKEND_URL = os.environ.get("FOCASSIST_BACKEND_URL", "")
BEARER_TOKEN = os.environ.get("FOCASSIST_TOKEN", "")

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _auth_headers() -> dict:
    return {**_HEADERS, "Authorization": f"Bearer {BEARER_TOKEN}"}


def _post(path: str, body: dict) -> dict:
    url = urljoin(BACKEND_URL, path)
    data = json.dumps(body).encode()
    req = Request(url, data=data, headers=_auth_headers(), method="POST")
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get(path: str) -> dict | list:
    url = urljoin(BACKEND_URL, path)
    req = Request(url, headers=_auth_headers())
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def push_aggregates(target_date: date, aggregates: list[dict], ambiguous: list[dict]) -> None:
    """Push today's aggregates and ambiguous queue to the backend."""
    if not BACKEND_URL:
        log.warning("FOCASSIST_BACKEND_URL not set — skipping push.")
        return
    payload = {
        "date": target_date.isoformat(),
        "aggregates": aggregates,
        "ambiguous": ambiguous,
    }
    try:
        _post("/ingest", payload)
        log.info("Pushed %d aggregates, %d ambiguous items for %s",
                 len(aggregates), len(ambiguous), target_date)
    except (URLError, HTTPError) as e:
        log.error("Failed to push aggregates: %s", e)


def get_directive() -> dict:
    """
    Poll the backend for the current focus-block directive.
    Returns a dict with keys: focus_block_active, block_domains, block_until.
    Falls back to a safe default (no block) on any error.
    """
    if not BACKEND_URL:
        return {"focus_block_active": False, "block_domains": [], "block_until": None}
    try:
        return _get("/directive")
    except (URLError, HTTPError) as e:
        log.error("Failed to get directive: %s", e)
        return {"focus_block_active": False, "block_domains": [], "block_until": None}


def get_rules() -> list[dict]:
    """
    Fetch the current Tier-1 ruleset from the backend.
    Returns [] on failure (caller falls back to seed rules).
    """
    if not BACKEND_URL:
        return []
    try:
        return _get("/rules")
    except (URLError, HTTPError) as e:
        log.error("Failed to fetch rules: %s", e)
        return []
