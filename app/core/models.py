"""Database tables (SQLAlchemy 2.0).

Every table has a CHECK constraint for the rules that must never be broken, on top of the
Pydantic validation in front of it. Table changes go through Alembic migrations.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal, get_args

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.core.schemas import AttentionThreadStatus, EntityType, MemoryKind, MessageRole, TaskStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def one_of(column: str, allowed) -> str:
    """SQL text for a CHECK constraint: column IN ('a', 'b', ...)."""
    values = ", ".join(f"'{v}'" for v in get_args(allowed))
    return f"{column} IN ({values})"


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- people


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    # Short "who is this person" summary, rebuilt from memories and read on every request.
    profile: Mapped[str] = mapped_column(Text, default="")
    profile_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


# ---------------------------------------------------------------- recent chat


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (Index("conversation_user_created", "user_id", "created_at"),)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(one_of("role", MessageRole), name="message_role_valid"),
        Index("message_conversation_created", "conversation_id", "created_at"),
    )


# ---------------------------------------------------------------- long-term memory
# The raw conversation lives in object storage. This table is the searchable index of what matters.


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # fact | preference | event | episode
    content: Mapped[str] = mapped_column(Text)  # one self-contained sentence or short paragraph
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 of normalised content
    importance: Mapped[int] = mapped_column(Integer, default=3)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_key: Mapped[str | None] = mapped_column(String(512))  # archive key of the source turn
    # Only filled when meaning-search is useful. `deferred`: never loaded unless asked for.
    embedding = mapped_column(Vector(settings.embedding_dim), nullable=True, deferred=True)
    # Drift protection: which model made the vector, and whether this memory should have one at all.
    # Search only compares vectors made by the current model; `reembed` brings the rest up to date.
    wants_embedding: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "content_hash", name="memory_user_content_unique"),
        CheckConstraint(one_of("kind", MemoryKind), name="memory_kind_valid"),
        CheckConstraint("importance BETWEEN 1 AND 5", name="memory_importance_range"),
        CheckConstraint("length(content) > 0", name="memory_content_not_empty"),
        CheckConstraint(
            "embedding IS NULL OR embedding_model IS NOT NULL", name="memory_vector_names_its_model"
        ),
        Index("memory_user_active_created", "user_id", "is_active", "created_at"),
        Index("memory_user_event", "user_id", "event_at"),
        Index("memory_source_key", "source_key"),
        # exact / keyword search
        Index("memory_fts_idx", text("to_tsvector('english', content)"), postgresql_using="gin"),
        # near-duplicate detection (pg_trgm)
        Index(
            "memory_content_trgm_idx",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ),
        # meaning search
        Index(
            "memory_embedding_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# ---------------------------------------------------------------- light knowledge graph
# Entity = a person, place, org, project. EntityRelation = "Riya --sister of--> Subhadip".


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(20), default="other")
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(one_of("type", EntityType), name="entity_type_valid"),
        # "riya" and "Riya" are the same person
        Index("entity_user_name_unique", "user_id", text("lower(name)"), unique=True),
    )


class MemoryEntity(Base):
    __tablename__ = "memory_entities"

    memory_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("memory_entity_entity", "entity_id"),)


class EntityRelation(Base):
    __tablename__ = "entity_relations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    subject_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    predicate: Mapped[str] = mapped_column(String(100))  # "sister of", "works at", "lives in"
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))

    __table_args__ = (
        UniqueConstraint("subject_id", "predicate", "target_id", name="relation_unique"),
        Index("relation_target", "target_id"),
    )


# ---------------------------------------------------------------- planner data


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    details: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default="open")
    created_at: Mapped[datetime] = created_at()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(one_of("status", TaskStatus), name="task_status_valid"),
        CheckConstraint("length(title) > 0", name="task_title_not_empty"),
        Index("task_user_status_due", "user_id", "status", "due_at"),
    )


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint("length(title) > 0", name="note_title_not_empty"),
        Index("note_user_created", "user_id", "created_at"),
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(String(300))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending / sent / cancelled
    created_at: Mapped[datetime] = created_at()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'sent', 'cancelled')", name="reminder_status_valid"),
        CheckConstraint("length(message) > 0", name="reminder_message_not_empty"),
        # only the waiting reminders are indexed, so "what is due next?" stays instant however many are sent
        Index("reminder_pending_due", "due_at", postgresql_where=text("status = 'pending'")),
    )


# ---------------------------------------------------------------- session learning
# At session end, the agent reflects on the conversation and saves operational lessons.


class SessionGuide(Base):
    __tablename__ = "session_guides"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    # What the agent learned about how to work with this user.
    learnings: Mapped[str] = mapped_column(Text)
    # How many messages were in the session when this was generated.
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint("length(learnings) > 0", name="guide_learnings_not_empty"),
        UniqueConstraint("conversation_id", name="guide_one_per_conversation"),
        Index("guide_user_created", "user_id", "created_at"),
    )


# ---------------------------------------------------------------- reflection brain
# Periodic synthesis of everything Riki knows about the user. Versioned: newest is active.


class UserReflection(Base):
    __tablename__ = "user_reflections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    # How many active memories existed when this was generated (staleness check).
    memory_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        Index("reflection_user_created", "user_id", "created_at"),
    )


# ---------------------------------------------------------------- proactive nudges
# Messages Riki sends without being asked — based on the reflection brain.


class ProactiveNudge(Base):
    __tablename__ = "proactive_nudges"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(20), default="open_loop")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attention_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attention_threads.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("nudge_user_sent", "user_id", "sent_at"),
        Index("nudge_telegram_msg", "telegram_message_id"),
    )


# ---------------------------------------------------------------- cognition cycles
# Each time the agent "thinks between conversations" — reviews what's new, updates its
# understanding, and decides whether to reach out.


class CognitionCycle(Base):
    __tablename__ = "cognition_cycles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reflection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_reflections.id", ondelete="SET NULL")
    )
    memories_reviewed: Mapped[int] = mapped_column(Integer, default=0)
    notices: Mapped[dict] = mapped_column(JSONB, default=list)
    monologue: Mapped[str] = mapped_column(Text, default="")
    reached_out: Mapped[bool] = mapped_column(Boolean, default=False)
    reach_out_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    reach_out_topic: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    phase_durations: Mapped[dict] = mapped_column(JSONB, default=dict)
    signals_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        Index("cognition_user_created", "user_id", "created_at"),
    )


# ---------------------------------------------------------------- user state model
# Structured, queryable, diffable representation of what the agent believes about the user.


class UserState(Base):
    __tablename__ = "user_states"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dimensions: Mapped[dict] = mapped_column(JSONB, default=dict)
    signals_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    diff_from_previous: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("state_user_computed", "user_id", "computed_at"),
    )


# ---------------------------------------------------------------- attention threads
# Explicit tracked hypotheses the agent is watching across cognition cycles.


class AttentionThread(Base):
    __tablename__ = "attention_threads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    topic: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="watching")
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSONB, default=list)
    trigger: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cognition_cycles.id", ondelete="SET NULL"), nullable=True
    )
    resolve_after_cycles: Mapped[int] = mapped_column(Integer, default=8)
    cycles_without_update: Mapped[int] = mapped_column(Integer, default=0)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    was_accurate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = created_at()
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint(one_of("status", AttentionThreadStatus), name="thread_status_valid"),
        Index("thread_user_status", "user_id", "status"),
        Index("thread_user_created", "user_id", "created_at"),
    )


# ---------------------------------------------------------------- habits
# Simple habit tracking: what the user wants to do regularly, and entries for each day.

HabitFrequency = Literal["daily", "weekday", "weekly"]


class Habit(Base):
    __tablename__ = "habits"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    frequency: Mapped[str] = mapped_column(String(20), default="daily")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="habit_unique_name"),
        Index("habit_user_active", "user_id", "active"),
        CheckConstraint("frequency IN ('daily', 'weekday', 'weekly')", name="habit_frequency_check"),
    )


class HabitEntry(Base):
    __tablename__ = "habit_entries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    habit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("habits.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    value: Mapped[str] = mapped_column(String(200), default="done")
    note: Mapped[str] = mapped_column(String(500), default="")
    logged_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        Index("entry_habit_date", "habit_id", "logged_for"),
        Index("entry_user_date", "user_id", "logged_for"),
    )


# ---------------------------------------------------------------- observability
# One ObsTrace per unit of work (a chat turn, a memory-extraction run, an operator run).
# ObsSpan rows are the steps inside it: model calls, tool calls, retrieval, embeddings.


class ObsTrace(Base):
    __tablename__ = "obs_traces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20))  # chat | extraction | operator
    source: Mapped[str] = mapped_column(String(20), default="")  # web | telegram | api | background
    conversation_id: Mapped[uuid.UUID | None] = mapped_column()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | error
    error: Mapped[str | None] = mapped_column(Text)
    user_text: Mapped[str | None] = mapped_column(Text)
    reply_text: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    recalled: Mapped[int] = mapped_column(Integer, default=0)  # memories handed to the model
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("obs_trace_started", "started_at"),
        Index("obs_trace_kind_started", "kind", "started_at"),
    )


class ObsSpan(Base):
    __tablename__ = "obs_spans"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    trace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("obs_traces.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # llm | tool | retrieval | recall | embed | step | db
    name: Mapped[str] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="ok")
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    attrs: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("obs_span_trace", "trace_id"),
        Index("obs_span_started", "started_at"),
        Index("obs_span_kind_name_started", "kind", "name", "started_at"),
    )
