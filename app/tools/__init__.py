"""Tool registry.

To add a tool: write an async function decorated with @tool in a file here, then append it
to the right list below. Nothing else needs to change.
"""

from app.tools.calendar import create_calendar_event, list_calendar_events, search_calendar_events
from app.tools.drive import read_drive_file, search_drive
from app.tools.gmail import create_email_draft, read_email, search_emails
from app.tools.habits import show_habits, stop_tracking_habit, track_habit
from app.tools.memory import forget_memory, remember, search_memory
from app.tools.reminders import cancel_reminder, list_reminders, set_reminder
from app.tools.planner import add_note, add_task, complete_task, list_tasks, search_notes
from app.tools.timeinfo import get_current_time
from app.tools.web import image_search, web_search

MEMORY_TOOLS = [remember, search_memory, forget_memory]
PLANNER_TOOLS = [
    get_current_time,
    add_task,
    list_tasks,
    complete_task,
    add_note,
    search_notes,
    list_calendar_events,
    search_calendar_events,
    create_calendar_event,
    set_reminder,
    list_reminders,
    cancel_reminder,
]
WORKSPACE_TOOLS = [get_current_time, search_emails, read_email, create_email_draft, search_drive, read_drive_file]
RESEARCH_TOOLS = [web_search, image_search]
HABIT_TOOLS = [track_habit, show_habits, stop_tracking_habit]

# Single agent gets every tool (deduplicated).
_seen = set()
ALL_TOOLS = []
for _t in PLANNER_TOOLS + WORKSPACE_TOOLS + RESEARCH_TOOLS + MEMORY_TOOLS + HABIT_TOOLS:
    if _t.name not in _seen:
        _seen.add(_t.name)
        ALL_TOOLS.append(_t)
del _seen, _t
