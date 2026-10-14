"""Habit tracking: create habits, log entries, check streaks."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, func

from app.core.db import session_scope
from app.core.models import Habit, HabitEntry


async def get_or_create_habit(user_id: UUID, name: str, frequency: str = "daily") -> Habit:
    name_lower = name.strip().lower()
    async with session_scope() as session:
        habit = await session.scalar(
            select(Habit).where(Habit.user_id == user_id, func.lower(Habit.name) == name_lower)
        )
        if habit:
            if not habit.active:
                habit.active = True
            return habit
        habit = Habit(user_id=user_id, name=name.strip(), frequency=frequency)
        session.add(habit)
        await session.flush()
        return habit


async def log_entry(user_id: UUID, habit_name: str, value: str = "done",
                    note: str = "", logged_for: datetime | None = None) -> HabitEntry:
    habit = await get_or_create_habit(user_id, habit_name)
    when = logged_for or datetime.now(timezone.utc)
    async with session_scope() as session:
        entry = HabitEntry(
            habit_id=habit.id, user_id=user_id,
            value=value, note=note, logged_for=when,
        )
        session.add(entry)
        await session.flush()
        return entry


async def list_habits(user_id: UUID, active_only: bool = True) -> list[Habit]:
    async with session_scope() as session:
        q = select(Habit).where(Habit.user_id == user_id)
        if active_only:
            q = q.where(Habit.active.is_(True))
        return list((await session.scalars(q.order_by(Habit.created_at))).all())


async def recent_entries(user_id: UUID, habit_id: UUID, days: int = 7) -> list[HabitEntry]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_scope() as session:
        return list((await session.scalars(
            select(HabitEntry)
            .where(HabitEntry.habit_id == habit_id, HabitEntry.logged_for >= cutoff)
            .order_by(HabitEntry.logged_for.desc())
        )).all())


async def deactivate_habit(user_id: UUID, habit_name: str) -> bool:
    name_lower = habit_name.strip().lower()
    async with session_scope() as session:
        habit = await session.scalar(
            select(Habit).where(Habit.user_id == user_id, func.lower(Habit.name) == name_lower, Habit.active.is_(True))
        )
        if not habit:
            return False
        habit.active = False
        return True


async def unlogged_today(user_id: UUID, tz: str) -> list[Habit]:
    """Return active daily/weekday habits not logged today (in user's timezone)."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(tz))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    weekday = now.weekday()  # 0=Mon, 6=Sun

    habits = await list_habits(user_id)
    missing = []
    for h in habits:
        if h.frequency == "weekday" and weekday >= 5:
            continue
        if h.frequency == "weekly":
            week_start = today_start - timedelta(days=weekday)
            cutoff = week_start
        else:
            cutoff = today_start

        async with session_scope() as session:
            logged = await session.scalar(
                select(HabitEntry.id).where(
                    HabitEntry.habit_id == h.id,
                    HabitEntry.logged_for >= cutoff,
                ).limit(1)
            )
        if not logged:
            missing.append(h)
    return missing
