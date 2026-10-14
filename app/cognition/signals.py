"""Computed signals: pure SQL queries that measure what's happening, so the LLM interprets instead of counts."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TypedDict
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, extract, case, and_

from app.config import settings
from app.core.db import session_scope
from app.core.models import (
    Conversation,
    Entity,
    Habit,
    HabitEntry,
    Memory,
    MemoryEntity,
    Message,
    Reminder,
    Task,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- typed results


class EntityFrequency(TypedDict):
    name: str
    type: str
    recent_count: int
    baseline_avg: float
    ratio: float
    is_spike: bool


class ActivityBucket(TypedDict):
    hour_range: str
    recent_count: int
    baseline_avg: float


class ActivityPattern(TypedDict):
    buckets: list[ActivityBucket]
    total_recent: int
    total_baseline: int
    shift_detected: bool


class MoodMemory(TypedDict):
    content: str
    kind: str
    importance: int


class MoodIndicator(TypedDict):
    date: str
    memories: list[MoodMemory]


class HabitStreak(TypedDict):
    habit_name: str
    frequency: str
    current_streak: int
    last_logged: str | None


class EntityRecency(TypedDict):
    name: str
    type: str
    last_seen: str
    days_since_last: int
    total_mentions: int
    status: str  # active | fading | resurfaced


class DueSoonTask(TypedDict):
    title: str
    due_at: str
    days_until: int


class OpenCommitments(TypedDict):
    open_tasks: int
    overdue_tasks: int
    due_soon: list[DueSoonTask]
    pending_reminders: int


class ConversationCadence(TypedDict):
    hours_since_last: float
    avg_gap_hours: float
    is_quiet: bool


# ---------------------------------------------------------------- signal functions


async def entity_frequency(
    user_id: UUID,
    recent_days: int | None = None,
    baseline_days: int | None = None,
) -> list[EntityFrequency]:
    recent_days = recent_days or settings.signal_entity_recent_days
    baseline_days = baseline_days or settings.signal_entity_baseline_days
    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=recent_days)
    baseline_cutoff = now - timedelta(days=baseline_days)

    async with session_scope() as session:
        # Recent counts per entity
        recent_q = (
            select(
                Entity.name,
                Entity.type,
                func.count().label("cnt"),
            )
            .select_from(MemoryEntity)
            .join(Entity, Entity.id == MemoryEntity.entity_id)
            .join(Memory, Memory.id == MemoryEntity.memory_id)
            .where(
                Memory.user_id == user_id,
                Memory.is_active.is_(True),
                Memory.created_at >= recent_cutoff,
            )
            .group_by(Entity.name, Entity.type)
        )
        recent_rows = (await session.execute(recent_q)).all()

        # Baseline counts per entity (full baseline window)
        baseline_q = (
            select(
                Entity.name,
                func.count().label("cnt"),
            )
            .select_from(MemoryEntity)
            .join(Entity, Entity.id == MemoryEntity.entity_id)
            .join(Memory, Memory.id == MemoryEntity.memory_id)
            .where(
                Memory.user_id == user_id,
                Memory.is_active.is_(True),
                Memory.created_at >= baseline_cutoff,
            )
            .group_by(Entity.name)
        )
        baseline_rows = (await session.execute(baseline_q)).all()

    baseline_map: dict[str, int] = {row.name: row.cnt for row in baseline_rows}
    scale = recent_days / max(baseline_days, 1)

    results: list[EntityFrequency] = []
    for row in recent_rows:
        baseline_total = baseline_map.get(row.name, 0)
        baseline_avg = baseline_total * scale
        ratio = row.cnt / max(baseline_avg, 0.1)
        results.append(EntityFrequency(
            name=row.name,
            type=row.type,
            recent_count=row.cnt,
            baseline_avg=round(baseline_avg, 2),
            ratio=round(ratio, 2),
            is_spike=ratio > 2.0 and row.cnt >= 3,
        ))
    results.sort(key=lambda x: x["recent_count"], reverse=True)
    return results


HOUR_BUCKETS = [(0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 24)]


async def activity_pattern(
    user_id: UUID,
    recent_days: int | None = None,
    baseline_days: int | None = None,
) -> ActivityPattern:
    recent_days = recent_days or settings.signal_activity_recent_days
    baseline_days = baseline_days or settings.signal_activity_baseline_days
    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=recent_days)
    baseline_cutoff = now - timedelta(days=baseline_days)

    hour_expr = extract("hour", Message.created_at)
    bucket_expr = (hour_expr / 4).cast(type_=func.integer if hasattr(func, "integer") else None)
    # Use floor division via case to stay DB-portable
    bucket_case = case(
        *[(and_(hour_expr >= lo, hour_expr < hi), idx) for idx, (lo, hi) in enumerate(HOUR_BUCKETS)],
        else_=5,
    )

    base_q = (
        select(bucket_case.label("bucket"), func.count().label("cnt"))
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == user_id, Message.role == "user")
        .group_by("bucket")
    )

    async with session_scope() as session:
        recent_rows = (await session.execute(
            base_q.where(Message.created_at >= recent_cutoff)
        )).all()
        baseline_rows = (await session.execute(
            base_q.where(Message.created_at >= baseline_cutoff)
        )).all()

    recent_map = {row.bucket: row.cnt for row in recent_rows}
    baseline_map = {row.bucket: row.cnt for row in baseline_rows}
    scale = recent_days / max(baseline_days, 1)

    buckets: list[ActivityBucket] = []
    shift_detected = False
    total_recent = 0
    total_baseline = 0

    for idx, (lo, hi) in enumerate(HOUR_BUCKETS):
        rc = recent_map.get(idx, 0)
        bc = baseline_map.get(idx, 0)
        baseline_avg = round(bc * scale, 2)
        total_recent += rc
        total_baseline += bc
        if baseline_avg > 0 and (rc > baseline_avg * 2 or rc < baseline_avg * 0.3):
            shift_detected = True
        buckets.append(ActivityBucket(
            hour_range=f"{lo:02d}-{hi:02d}",
            recent_count=rc,
            baseline_avg=baseline_avg,
        ))

    return ActivityPattern(
        buckets=buckets,
        total_recent=total_recent,
        total_baseline=total_baseline,
        shift_detected=shift_detected,
    )


async def mood_indicators(user_id: UUID, days: int | None = None) -> list[MoodIndicator]:
    days = days or settings.signal_mood_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(Memory)
                .where(
                    Memory.user_id == user_id,
                    Memory.is_active.is_(True),
                    Memory.kind.in_(["episode", "preference"]),
                    Memory.wants_embedding.is_(True),
                    Memory.created_at >= cutoff,
                )
                .order_by(Memory.created_at)
            )
        ).all()

    by_date: dict[str, list[MoodMemory]] = {}
    for m in rows:
        d = m.created_at.strftime("%Y-%m-%d")
        by_date.setdefault(d, []).append(MoodMemory(
            content=m.content,
            kind=m.kind,
            importance=m.importance,
        ))

    return [MoodIndicator(date=d, memories=mems) for d, mems in sorted(by_date.items())]


async def habit_streaks(user_id: UUID, timezone_str: str) -> list[HabitStreak]:
    tz = ZoneInfo(timezone_str)
    today = datetime.now(tz).date()

    async with session_scope() as session:
        habits = (
            await session.scalars(
                select(Habit).where(Habit.user_id == user_id, Habit.active.is_(True))
            )
        ).all()

    results: list[HabitStreak] = []
    for habit in habits:
        async with session_scope() as session:
            entries = (
                await session.scalars(
                    select(HabitEntry)
                    .where(HabitEntry.habit_id == habit.id)
                    .order_by(HabitEntry.logged_for.desc())
                    .limit(90)
                )
            ).all()

        logged_dates = set()
        last_logged_str: str | None = None
        for e in entries:
            d = e.logged_for.astimezone(tz).date()
            logged_dates.add(d)
            if last_logged_str is None:
                last_logged_str = d.isoformat()

        streak = 0
        check = today
        while True:
            if habit.frequency == "weekday" and check.weekday() >= 5:
                check -= timedelta(days=1)
                continue
            if habit.frequency == "weekly":
                week_dates = [check - timedelta(days=i) for i in range(7)]
                if any(wd in logged_dates for wd in week_dates):
                    streak += 1
                    check -= timedelta(days=7)
                    continue
                else:
                    break
            if check in logged_dates:
                streak += 1
                check -= timedelta(days=1)
            else:
                break

        results.append(HabitStreak(
            habit_name=habit.name,
            frequency=habit.frequency,
            current_streak=streak,
            last_logged=last_logged_str,
        ))

    return results


async def entity_recency(user_id: UUID, days: int = 30) -> list[EntityRecency]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    async with session_scope() as session:
        q = (
            select(
                Entity.name,
                Entity.type,
                func.max(Memory.created_at).label("last_seen"),
                func.count().label("mention_count"),
            )
            .select_from(MemoryEntity)
            .join(Entity, Entity.id == MemoryEntity.entity_id)
            .join(Memory, Memory.id == MemoryEntity.memory_id)
            .where(
                Memory.user_id == user_id,
                Memory.is_active.is_(True),
                Memory.created_at >= cutoff,
            )
            .group_by(Entity.name, Entity.type)
            .order_by(func.max(Memory.created_at).desc())
        )
        rows = (await session.execute(q)).all()

    results: list[EntityRecency] = []
    for row in rows:
        days_since = (now - row.last_seen).days
        avg_gap = days / max(row.mention_count, 1)
        recently_seen = days_since <= 3
        was_quiet = avg_gap > 0 and days_since > avg_gap * 2

        if recently_seen and was_quiet and row.mention_count > 1:
            status = "resurfaced"
        elif was_quiet:
            status = "fading"
        else:
            status = "active"

        results.append(EntityRecency(
            name=row.name,
            type=row.type,
            last_seen=row.last_seen.strftime("%Y-%m-%d"),
            days_since_last=days_since,
            total_mentions=row.mention_count,
            status=status,
        ))

    return results


async def open_commitments(user_id: UUID) -> OpenCommitments:
    now = datetime.now(timezone.utc)
    soon_cutoff = now + timedelta(hours=72)

    async with session_scope() as session:
        open_count = await session.scalar(
            select(func.count()).select_from(Task).where(
                Task.user_id == user_id, Task.status == "open"
            )
        ) or 0

        overdue_count = await session.scalar(
            select(func.count()).select_from(Task).where(
                Task.user_id == user_id,
                Task.status == "open",
                Task.due_at.isnot(None),
                Task.due_at < now,
            )
        ) or 0

        due_soon_rows = (
            await session.scalars(
                select(Task).where(
                    Task.user_id == user_id,
                    Task.status == "open",
                    Task.due_at.isnot(None),
                    Task.due_at >= now,
                    Task.due_at <= soon_cutoff,
                )
                .order_by(Task.due_at)
            )
        ).all()

        pending_reminders = await session.scalar(
            select(func.count()).select_from(Reminder).where(
                Reminder.user_id == user_id, Reminder.status == "pending"
            )
        ) or 0

    due_soon: list[DueSoonTask] = []
    for t in due_soon_rows:
        days_until = max(0, (t.due_at - now).days)
        due_soon.append(DueSoonTask(
            title=t.title,
            due_at=t.due_at.isoformat(),
            days_until=days_until,
        ))

    return OpenCommitments(
        open_tasks=open_count,
        overdue_tasks=overdue_count,
        due_soon=due_soon,
        pending_reminders=pending_reminders,
    )


async def conversation_cadence(user_id: UUID) -> ConversationCadence:
    now = datetime.now(timezone.utc)
    window = now - timedelta(days=30)

    async with session_scope() as session:
        recent_convos = (
            await session.scalars(
                select(Conversation)
                .where(Conversation.user_id == user_id, Conversation.created_at >= window)
                .order_by(Conversation.created_at.desc())
                .limit(50)
            )
        ).all()

    if not recent_convos:
        return ConversationCadence(hours_since_last=999.0, avg_gap_hours=0.0, is_quiet=True)

    latest = recent_convos[0].created_at
    hours_since = (now - latest).total_seconds() / 3600

    if len(recent_convos) < 2:
        return ConversationCadence(
            hours_since_last=round(hours_since, 1),
            avg_gap_hours=0.0,
            is_quiet=False,
        )

    gaps = []
    for i in range(len(recent_convos) - 1):
        gap = (recent_convos[i].created_at - recent_convos[i + 1].created_at).total_seconds() / 3600
        gaps.append(gap)
    avg_gap = sum(gaps) / len(gaps) if gaps else 0.0

    return ConversationCadence(
        hours_since_last=round(hours_since, 1),
        avg_gap_hours=round(avg_gap, 1),
        is_quiet=avg_gap > 0 and hours_since > avg_gap * 1.5,
    )


# ---------------------------------------------------------------- aggregate


async def compute_all_signals(user_id: UUID, timezone_str: str) -> dict:
    async def _safe(name: str, coro):
        try:
            result = await coro
            if isinstance(result, list):
                return name, [dict(r) if isinstance(r, dict) else r for r in result]
            return name, dict(result) if isinstance(result, dict) else result
        except Exception as e:
            log.warning("Signal %s failed: %s", name, e)
            return name, {"error": str(e)}

    tasks = [
        _safe("entity_frequency", entity_frequency(user_id)),
        _safe("activity_pattern", activity_pattern(user_id)),
        _safe("mood_indicators", mood_indicators(user_id)),
        _safe("habit_streaks", habit_streaks(user_id, timezone_str)),
        _safe("entity_recency", entity_recency(user_id)),
        _safe("open_commitments", open_commitments(user_id)),
        _safe("conversation_cadence", conversation_cadence(user_id)),
    ]
    results = await asyncio.gather(*tasks)
    return {name: value for name, value in results}
