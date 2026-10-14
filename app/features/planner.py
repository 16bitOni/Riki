"""Tasks and notes. Every write takes a validated Pydantic model."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update

from app.core.db import session_scope
from app.core.models import Note, Task
from app.core.schemas import NoteCreate, TaskCreate, TaskStatus


async def create_task(data: TaskCreate) -> Task:
    async with session_scope() as session:
        task = Task(user_id=data.user_id, title=data.title, details=data.details, due_at=data.due_at)
        session.add(task)
        await session.flush()
        return task


async def list_tasks(user_id: UUID, status: TaskStatus = "open") -> list[Task]:
    async with session_scope() as session:
        rows = await session.scalars(
            select(Task)
            .where(Task.user_id == user_id, Task.status == status)
            .order_by(Task.due_at.asc().nulls_last(), Task.created_at.asc())
        )
        return list(rows.all())


async def complete_task(user_id: UUID, task_id: UUID) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.user_id == user_id)
            .values(status="done", completed_at=datetime.now(timezone.utc))
        )
        return result.rowcount > 0


async def create_note(data: NoteCreate) -> Note:
    async with session_scope() as session:
        note = Note(user_id=data.user_id, title=data.title, body=data.body)
        session.add(note)
        await session.flush()
        return note


async def search_notes(user_id: UUID, text: str, limit: int = 10) -> list[Note]:
    async with session_scope() as session:
        rows = await session.scalars(
            select(Note)
            .where(
                Note.user_id == user_id,
                Note.title.icontains(text, autoescape=True) | Note.body.icontains(text, autoescape=True),
            )
            .order_by(Note.created_at.desc())
            .limit(limit)
        )
        return list(rows.all())
