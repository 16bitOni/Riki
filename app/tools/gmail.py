"""Gmail tools: read and search mail, create drafts. There is deliberately no send tool.

The Google client library is synchronous, so each call runs in a worker thread.
"""

import asyncio
import base64
from email.message import EmailMessage

from googleapiclient.errors import HttpError
from langchain_core.tools import tool

from app.core.privacy import tag_untrusted
from app.tools.google_common import MISSING_ACCESS, NOT_CONNECTED, google_service

MAX_BODY_CHARS = 4000


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _plain_text(payload: dict) -> str:
    """Walk the MIME tree and return the first text/plain part."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = _plain_text(part)
        if text:
            return text
    return ""


def _search(query: str, max_results: int) -> str:
    service = google_service("gmail", "v1")
    if service is None or isinstance(service, str):
        return service or NOT_CONNECTED
    refs = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute().get("messages", [])
    lines = []
    for ref in refs:
        m = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        lines.append(
            f"- id={m['id']} | {_header(m, 'Date')} | from {_header(m, 'From')} | "
            f"{_header(m, 'Subject') or '(no subject)'} | {m.get('snippet', '')[:120]}"
        )
    return "\n".join(lines) or "No emails matched."


def _read(message_id: str) -> str:
    service = google_service("gmail", "v1")
    if service is None or isinstance(service, str):
        return service or NOT_CONNECTED
    m = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    body = _plain_text(m["payload"]) or m.get("snippet", "")
    return (
        f"From: {_header(m, 'From')}\nTo: {_header(m, 'To')}\nDate: {_header(m, 'Date')}\n"
        f"Subject: {_header(m, 'Subject')}\n\n{body[:MAX_BODY_CHARS]}"
    )


def _draft(to: str, subject: str, body: str) -> str:
    service = google_service("gmail", "v1")
    if service is None or isinstance(service, str):
        return service or NOT_CONNECTED
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return f"Draft saved in Gmail (id {draft['id']}). It has NOT been sent; the user must review and send it."


def _guarded(fn, *args) -> str:
    try:
        return fn(*args)
    except HttpError as e:
        return MISSING_ACCESS if e.resp.status in (401, 403) else f"Gmail error: {e}"


@tool
async def search_emails(query: str = "in:inbox", max_results: int = 10) -> str:
    """Search Gmail with Gmail search syntax (e.g. 'is:unread', 'from:alice', 'subject:invoice newer_than:7d').
    Returns id, date, sender, subject and a snippet per email. Use read_email for the full text."""
    return await asyncio.to_thread(_guarded, _search, query, min(max(max_results, 1), 25))


@tool
async def read_email(message_id: str) -> str:
    """Read one email's headers and plain-text body by its id (from search_emails)."""
    result = await asyncio.to_thread(_guarded, _read, message_id)
    return tag_untrusted(result) if result and not result.startswith(("Google", "Gmail")) else result


@tool
async def create_email_draft(to: str, subject: str, body: str) -> str:
    """Save a draft email in Gmail. It is never sent; the user reviews and sends it themselves."""
    return await asyncio.to_thread(_guarded, _draft, to, subject, body)
