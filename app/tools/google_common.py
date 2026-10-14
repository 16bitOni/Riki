"""Shared Google credentials for the Calendar, Gmail and Drive tools. Connect once with:  python -m scripts.google_auth"""

import logging
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import settings

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # drafts only; no send tool is exposed
    "https://www.googleapis.com/auth/drive.readonly",
]
NOT_CONNECTED = "Google is not connected yet. Run `python -m scripts.google_auth` once."
TOKEN_EXPIRED = "Google credentials have expired or been revoked. Run `python -m scripts.google_auth` again to reconnect."
MISSING_ACCESS = "The Google login lacks access to this. Delete the token file and run `python -m scripts.google_auth` again."


def google_service(name: str, version: str):
    """Build an API client, or None if there is no token yet. Returns a string on auth failure."""
    token_path = Path(settings.google_token_path)
    if not token_path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(token_path))
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        except RefreshError as e:
            log.warning("Google token refresh failed: %s", e)
            return TOKEN_EXPIRED
    return build(name, version, credentials=creds, cache_discovery=False)
