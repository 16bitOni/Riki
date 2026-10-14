"""Shared utilities for inspecting graph output in eval tests."""

from langchain_core.messages import BaseMessage


def tool_called(messages: list[BaseMessage], name: str) -> bool:
    """True if any agent called the named tool."""
    for m in messages:
        for tc in getattr(m, "tool_calls", []):
            if tc["name"] == name:
                return True
    return False


def any_tool_called(messages: list[BaseMessage], names: set[str]) -> bool:
    """True if any of the named tools was called."""
    for m in messages:
        for tc in getattr(m, "tool_calls", []):
            if tc["name"] in names:
                return True
    return False


def tool_args(messages: list[BaseMessage], name: str) -> dict | None:
    """Return the args of the first call to the named tool, or None."""
    for m in messages:
        for tc in getattr(m, "tool_calls", []):
            if tc["name"] == name:
                return tc["args"]
    return None


def called_tools(messages: list[BaseMessage]) -> list[str]:
    """Return the names of all tools called, in order."""
    result = []
    for m in messages:
        for tc in getattr(m, "tool_calls", []):
            result.append(tc["name"])
    return result


def final_text(messages: list[BaseMessage]) -> str:
    """The last non-empty AI text in the conversation."""
    from app.llm import text_of
    for m in reversed(messages):
        if m.type == "ai" and not getattr(m, "tool_calls", None):
            t = text_of(m).strip()
            if t:
                return t
    return ""


# Legacy aliases for backwards compatibility during transition
def specialist_called(messages: list[BaseMessage], name: str) -> bool:
    """Compatibility shim: maps old specialist names to their tool sets."""
    SPECIALIST_TOOLS = {
        "planner": {"get_current_time", "add_task", "list_tasks", "complete_task",
                     "add_note", "search_notes", "list_calendar_events",
                     "search_calendar_events", "create_calendar_event",
                     "set_reminder", "list_reminders", "cancel_reminder"},
        "workspace": {"search_emails", "read_email", "create_email_draft",
                       "search_drive", "read_drive_file"},
        "researcher": {"web_search", "image_search"},
        "memory_keeper": {"remember", "search_memory", "forget_memory"},
    }
    tools = SPECIALIST_TOOLS.get(name, set())
    return any_tool_called(messages, tools)


def any_specialist_called(messages: list[BaseMessage]) -> bool:
    """True if any tool was called (i.e., the agent didn't answer directly)."""
    for m in messages:
        if getattr(m, "tool_calls", None):
            return True
    return False


def which_specialist(messages: list[BaseMessage]) -> str | None:
    """Return the first 'specialist area' a tool call maps to, or None."""
    TOOL_TO_AREA = {}
    for area, tools in {
        "planner": {"get_current_time", "add_task", "list_tasks", "complete_task",
                     "add_note", "search_notes", "list_calendar_events",
                     "search_calendar_events", "create_calendar_event",
                     "set_reminder", "list_reminders", "cancel_reminder"},
        "workspace": {"search_emails", "read_email", "create_email_draft",
                       "search_drive", "read_drive_file"},
        "researcher": {"web_search", "image_search"},
        "memory_keeper": {"remember", "search_memory", "forget_memory"},
    }.items():
        for t in tools:
            TOOL_TO_AREA[t] = area
    for m in messages:
        for tc in getattr(m, "tool_calls", []):
            if tc["name"] in TOOL_TO_AREA:
                return TOOL_TO_AREA[tc["name"]]
    return None
