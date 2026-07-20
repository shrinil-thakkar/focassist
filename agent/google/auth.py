"""Shared Google OAuth credential loading for calendar and Gmail tools.

Both tools authorize against the same GCP OAuth app and cache a single
token, so they must request all scopes together — asking for a new scope
later than the cached token supports is a common source of silent 403s.
"""

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# --- config -------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_DIR = Path(os.environ.get("FOCASSIST_DIR", Path.home() / ".focassist"))
CREDENTIALS_FILE = str(_DIR / "credentials.json")
TOKEN_FILE = str(_DIR / "token.json")


def get_credentials():
    _DIR.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"Missing {CREDENTIALS_FILE}. Download the OAuth client secret from the "
            "GCP console and save it there."
        )

    creds = None
    if os.path.exists(TOKEN_FILE):
        # Credentials.from_authorized_user_file() echoes back whatever `scopes`
        # you pass it rather than what's actually stored on disk, so check the
        # raw file for the granted scopes before trusting the cached token. A
        # token saved before this file's SCOPES list grew (e.g. Gmail added
        # after Calendar was already authorized) won't carry the new scope —
        # force re-consent rather than silently running with too little access.
        with open(TOKEN_FILE) as f:
            granted_scopes = set(json.load(f).get("scopes") or [])
        if set(SCOPES).issubset(granted_scopes):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds
