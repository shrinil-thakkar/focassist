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


def push_aggregates(
    target_date: date,
    aggregates: list[dict],
    ambiguous: list[dict],
    sessions: list[dict] | None = None,
    timeline: list[str] | None = None,
    hourly: list[dict] | None = None,
    coverage: dict | None = None,
) -> None:
    """
    Push today's aggregates, sessions, timeline, hourly rollup and coverage
    (active/idle/untracked totals + health flags, tracking-algorithm.md §5-6)
    to the backend.
    """
    if not BACKEND_URL:
        log.warning("FOCASSIST_BACKEND_URL not set — skipping push.")
        return
    payload = {
        "date": target_date.isoformat(),
        "aggregates": aggregates,
        "ambiguous": ambiguous,
        "sessions": sessions or [],
        "timeline": timeline or [],
        "hourly_activity": hourly or [],
        "coverage": coverage,
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


def get_reprocess_jobs() -> list[str]:
    """Return pending reprocess dates queued by the bot."""
    if not BACKEND_URL:
        return []
    try:
        result = _get("/reprocess-jobs")
        return result.get("dates", [])
    except (URLError, HTTPError) as e:
        log.error("Failed to fetch reprocess jobs: %s", e)
        return []


def mark_reprocess_done(job_date: str) -> None:
    if not BACKEND_URL:
        return
    try:
        _post(f"/reprocess-jobs/{job_date}/done", {})
    except (URLError, HTTPError) as e:
        log.error("Failed to mark reprocess done for %s: %s", job_date, e)


def get_fetch_jobs() -> list[dict]:
    """Return pending Gmail+Calendar fetch jobs queued by the /fetch bot command."""
    if not BACKEND_URL:
        return []
    try:
        result = _get("/fetch-jobs")
        return result.get("jobs", [])
    except (URLError, HTTPError) as e:
        log.error("Failed to fetch jobs: %s", e)
        return []


def mark_fetch_job_done(job_id: int) -> None:
    if not BACKEND_URL:
        return
    try:
        _post(f"/fetch-jobs/{job_id}/done", {})
    except (URLError, HTTPError) as e:
        log.error("Failed to mark fetch job %s done: %s", job_id, e)
