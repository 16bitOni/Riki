"""Google Calendar tools. Connect once with:  python -m scripts.google_auth

The Google client library is synchronous, so each call runs in a worker thread.
"""

import asyncio
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.core.timeutil import now_in, parse_iso
from app.tools.google_common import NOT_CONNECTED, google_service


def _service():
    return google_service("calendar", "v3")


def _list_events(start: str, end: str, query: str | None = None) -> str:
    service = _service()
    if service is None or isinstance(service, str):
        return service or NOT_CONNECTED
    params = dict(calendarId="primary", timeMin=start, timeMax=end, singleEvents=True, orderBy="startTime")
    if query:
        params["q"] = query
    items = service.events().list(**params).execute().get("items", [])
    lines = [
        f"- {e.get('summary', '(no title)')}: "
        f"{e['start'].get('dateTime', e['start'].get('date'))}"
        for e in items
    ]
    return "\n".join(lines) or "No events in that period."


def _create_event(title: str, start: str, end: str, timezone: str, description: str) -> str:
    service = _service()
    if service is None or isinstance(service, str):
        return service or NOT_CONNECTED
    event = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
    created = service.events().insert(calendarId="primary", body=event).execute()
    return f"Event created: {created.get('htmlLink')}"


@tool
async def list_calendar_events(
    start_date: str | None = None,
    end_date: str | None = None,
    days_ahead: int = 7,
    *,
    config: RunnableConfig,
) -> str:
    """List Google Calendar events for a date range.

    Prefer start_date / end_date (YYYY-MM-DD) for named periods like "December" or
    "next week". Use days_ahead only when no specific range is needed.

    Examples:
      - "December" → start_date="2026-12-01", end_date="2026-12-31"
      - "this week" → start_date="2026-10-13", end_date="2026-10-19"
      - "next 7 days" → days_ahead=7 (start_date/end_date omitted)
    """
    user = config["configurable"]["user"]
    now = now_in(user.timezone)
    if start_date:
        try:
            tz = ZoneInfo(user.timezone)
            t_start = date.fromisoformat(start_date)
            t_end = date.fromisoformat(end_date) if end_date else t_start + timedelta(days=1)
            start_iso = datetime.combine(t_start, dt_time.min, tzinfo=tz).isoformat()
            end_iso = datetime.combine(t_end + timedelta(days=1), dt_time.min, tzinfo=tz).isoformat()
        except ValueError:
            return "Invalid date format. Use YYYY-MM-DD."
    else:
        start_iso = now.isoformat()
        end_iso = (now + timedelta(days=days_ahead)).isoformat()
    return await asyncio.to_thread(_list_events, start_iso, end_iso)


@tool
async def search_calendar_events(
    query: str,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    config: RunnableConfig,
) -> str:
    """Search Google Calendar events by keyword (name, person, place).

    Use this when you want to find events matching a word or name rather than
    listing everything in a range. start_date/end_date (YYYY-MM-DD) narrow the
    window; omit them to search across the next 365 days.

    Examples:
      - find Subhodip's birthday → query="Subhodip"
      - find dentist appointments → query="dentist"
      - find flights in December → query="flight", start_date="2026-12-01", end_date="2026-12-31"
    """
    user = config["configurable"]["user"]
    now = now_in(user.timezone)
    if start_date:
        try:
            tz = ZoneInfo(user.timezone)
            t_start = date.fromisoformat(start_date)
            t_end = date.fromisoformat(end_date) if end_date else t_start + timedelta(days=366)
            start_iso = datetime.combine(t_start, dt_time.min, tzinfo=tz).isoformat()
            end_iso = datetime.combine(t_end + timedelta(days=1), dt_time.min, tzinfo=tz).isoformat()
        except ValueError:
            return "Invalid date format. Use YYYY-MM-DD."
    else:
        start_iso = now.isoformat()
        end_iso = (now + timedelta(days=365)).isoformat()
    return await asyncio.to_thread(_list_events, start_iso, end_iso, query)


@tool
async def create_calendar_event(
    title: str, start: str, end: str | None = None, description: str = "", *, config: RunnableConfig
) -> str:
    """Create a Google Calendar event. start/end are ISO 8601; if end is omitted the event lasts one hour."""
    user = config["configurable"]["user"]
    try:
        start_dt = parse_iso(start, user.timezone)
        end_dt = parse_iso(end, user.timezone)
    except ValueError:
        return "Could not read the date/time. Use ISO 8601."
    if start_dt is None:
        return "A start time is required."
    end_dt = end_dt or start_dt + timedelta(hours=1)
    return await asyncio.to_thread(
        _create_event, title, start_dt.isoformat(), end_dt.isoformat(), user.timezone, description
    )
