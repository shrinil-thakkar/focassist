"""Gmail tool — fetches recent sent/received email for the weekly review.

Mirrors agent/calendar.py's shape: one fetch function returning plain data,
no AI here. Read-only (gmail.readonly scope) — nothing here can send,
modify, or delete mail.
"""

import base64
import re
import sys

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from agent.google_auth import get_credentials

BODY_CHAR_LIMIT = 5000


# --- auth ---------------------------------------------------------------
def _service():
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds)


# --- body extraction ------------------------------------------------------
def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Recursively walk MIME parts, preferring text/plain, falling back to text/html."""
    plain, html = _find_parts(payload)
    if plain:
        return plain[:BODY_CHAR_LIMIT]
    if html:
        return _strip_html(html)[:BODY_CHAR_LIMIT]
    return ""


def _find_parts(part: dict):
    """Returns (plain_text_or_None, html_text_or_None) found anywhere under `part`."""
    mime = part.get("mimeType", "")
    body_data = part.get("body", {}).get("data")

    if mime == "text/plain" and body_data:
        return _decode(body_data), None
    if mime == "text/html" and body_data:
        return None, _decode(body_data)

    plain_found, html_found = None, None
    for sub in part.get("parts", []) or []:
        plain, html = _find_parts(sub)
        if plain and not plain_found:
            plain_found = plain
        if html and not html_found:
            html_found = html
    return plain_found, html_found


def _header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# --- fetching ---------------------------------------------------------------
def fetch_emails(days: int = 7, max_results: int = 200) -> list:
    """Sent + received emails from the last `days` days, newest first, capped at `max_results`."""
    service = _service()
    query = f"newer_than:{days}d (in:inbox OR in:sent)"

    message_ids = []
    page_token = None
    while len(message_ids) < max_results:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token)
            .execute()
        )
        message_ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    message_ids = message_ids[:max_results]

    total = len(message_ids)
    emails = []
    for i, msg_id in enumerate(message_ids, 1):
        print(f"  fetching email {i}/{total}...", file=sys.stderr, end="\r")
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except HttpError as e:
            if e.resp.status == 403:
                raise PermissionError(
                    "Gmail API returned 403 (insufficient scope). Delete "
                    "~/.focassist/token.json and re-run to re-consent with the "
                    "gmail.readonly scope."
                ) from e
            raise

        headers = msg.get("payload", {}).get("headers", [])
        label_ids = msg.get("labelIds", [])
        emails.append({
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "direction": "sent" if "SENT" in label_ids else "received",
            "date": _header(headers, "Date"),
            "from": _header(headers, "From"),
            "to": _header(headers, "To"),
            "subject": _header(headers, "Subject"),
            "snippet": msg.get("snippet", ""),
            "body": _extract_body(msg.get("payload", {})),
        })
    print(file=sys.stderr)  # clear the progress line
    # messages().list already returns newest first; emails list preserves that order.

    sent = sum(1 for e in emails if e["direction"] == "sent")
    received = total - sent
    print(f"Fetched {total} emails ({sent} sent, {received} received)", file=sys.stderr)

    return emails
