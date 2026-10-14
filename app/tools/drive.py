"""Google Drive tools (read-only): find files and read their text.

The Google client library is synchronous, so each call runs in a worker thread.
"""

import asyncio

from googleapiclient.errors import HttpError
from langchain_core.tools import tool

from app.core.privacy import tag_untrusted
from app.tools.google_common import MISSING_ACCESS, NOT_CONNECTED, google_service

MAX_CHARS = 6000
GOOGLE_EXPORTS = {  # Google-native files must be exported to a readable format
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}


def _search(query: str, max_results: int) -> str:
    service = google_service("drive", "v3")
    if service is None or isinstance(service, str):
        return service or NOT_CONNECTED
    safe = query.replace("\\", "\\\\").replace("'", "\\'")
    files = (
        service.files()
        .list(
            q=f"fullText contains '{safe}' and trashed = false",
            pageSize=max_results,
            fields="files(id,name,mimeType,modifiedTime)",
            orderBy="modifiedTime desc",
        )
        .execute()
        .get("files", [])
    )
    lines = [f"- id={f['id']} | {f['name']} | {f['mimeType']} | modified {f['modifiedTime']}" for f in files]
    return "\n".join(lines) or "No files matched."


def _read(file_id: str) -> str:
    service = google_service("drive", "v3")
    if service is None or isinstance(service, str):
        return service or NOT_CONNECTED
    meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
    mime = meta["mimeType"]
    if mime in GOOGLE_EXPORTS:
        data = service.files().export(fileId=file_id, mimeType=GOOGLE_EXPORTS[mime]).execute()
    elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
        data = service.files().get_media(fileId=file_id).execute()
    else:
        return f"'{meta['name']}' is a {mime} file; only Google Docs, Sheets, Slides and text files can be read."
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    note = "\n[truncated]" if len(text) > MAX_CHARS else ""
    return f"{meta['name']}\n\n{text[:MAX_CHARS]}{note}"


def _guarded(fn, *args) -> str:
    try:
        return fn(*args)
    except HttpError as e:
        return MISSING_ACCESS if e.resp.status in (401, 403) else f"Drive error: {e}"


@tool
async def search_drive(query: str, max_results: int = 10) -> str:
    """Search Google Drive file contents and names for the given words. Returns id, name, type, modified time."""
    return await asyncio.to_thread(_guarded, _search, query, min(max(max_results, 1), 25))


@tool
async def read_drive_file(file_id: str) -> str:
    """Read the text of a Drive file by id (from search_drive). Works for Docs, Sheets, Slides and text files."""
    result = await asyncio.to_thread(_guarded, _read, file_id)
    return tag_untrusted(result) if result and not result.startswith(("Google", "Drive")) else result
