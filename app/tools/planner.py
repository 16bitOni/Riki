"""Tools for the planner agent: tasks and notes.

Argument types are checked by Pydantic before the function runs; the planner module then
validates again with a strict model before anything is written.
"""

from datetime import datetime
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.features import planner
from app.core.schemas import Details, NoteBody, NoteCreate, SearchText, ShortText, TaskCreate, TaskStatus
from app.core.timeutil import localize


def _describe(task) -> str:
    due = f", due {task.due_at.strftime('%Y-%m-%d %H:%M')}" if task.due_at else ""
    return f"- {task.title}{due}  (id: {task.id}, {task.status})"


@tool
async def add_task(
    title: ShortText,
    due: datetime | None = None,
    details: Details = "",
    *,
    config: RunnableConfig,
) -> str:
    """Add a to-do item. due is an optional ISO 8601 date/time, e.g. 2026-10-09T18:00:00+05:30."""
    user = config["configurable"]["user"]
    task = await planner.create_task(
        TaskCreate(user_id=user.id, title=title, details=details, due_at=localize(due, user.timezone))
    )
    return f"Task added (id: {task.id})."


@tool
async def list_tasks(status: TaskStatus = "open", *, config: RunnableConfig) -> str:
    """List tasks, soonest due first."""
    user = config["configurable"]["user"]
    tasks = await planner.list_tasks(user.id, status)
    return "\n".join(_describe(t) for t in tasks) or f"No {status} tasks."


@tool
async def complete_task(task_id: UUID, *, config: RunnableConfig) -> str:
    """Mark a task as done, using the id shown by list_tasks."""
    user = config["configurable"]["user"]
    done = await planner.complete_task(user.id, task_id)
    return "Marked done." if done else "No task with that id."


@tool
async def add_note(title: ShortText, body: NoteBody, *, config: RunnableConfig) -> str:
    """Save a note (longer free text such as meeting notes, ideas, drafts)."""
    user = config["configurable"]["user"]
    note = await planner.create_note(NoteCreate(user_id=user.id, title=title, body=body))
    return f"Note saved (id: {note.id})."


@tool
async def search_notes(query: SearchText, *, config: RunnableConfig) -> str:
    """Find notes whose title or body contains the text."""
    user = config["configurable"]["user"]
    notes = await planner.search_notes(user.id, query)
    return "\n\n".join(f"{n.title} ({n.created_at:%Y-%m-%d})\n{n.body}" for n in notes) or "No notes found."
