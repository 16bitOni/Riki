"""Pydantic models: the gatekeepers.

Nothing reaches the database, and no request is accepted, unless it passes through one of these.
The database repeats the most important rules as CHECK constraints (see models.py), so a bug
in this file still cannot put bad data in a table.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

# ---- allowed values (shared by validation and by the database CHECK constraints)
MemoryKind = Literal["fact", "preference", "event", "episode"]
EntityType = Literal["person", "place", "org", "project", "other"]
TaskStatus = Literal["open", "done"]
AttentionThreadStatus = Literal["watching", "resolved", "abandoned"]
MessageRole = Literal["user", "assistant"]

# ---- reusable text types: whitespace trimmed, empty rejected, length capped
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
MemoryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
SearchText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Details = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
NoteBody = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
ChatText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Importance = Annotated[int, Field(ge=1, le=5, description="1 = trivia ... 5 = core to who the user is")]


class Strict(BaseModel):
    """Base for everything that gets written: unknown fields are an error, not ignored."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- writes (planner)


class TaskCreate(Strict):
    user_id: UUID
    title: ShortText
    details: Details = ""
    due_at: AwareDatetime | None = None  # must carry a timezone


class NoteCreate(Strict):
    user_id: UUID
    title: ShortText
    body: NoteBody


# ---------------------------------------------------------------- API in / out


class ImageInput(BaseModel):
    """An image the user attached to their message. Exactly one of `url` or `base64` must be set."""

    url: str | None = None
    base64: str | None = None
    media_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp"] = "image/jpeg"


class ChatRequest(Strict):
    message: ChatText
    conversation_id: UUID | None = None
    images: list[ImageInput] = Field(default_factory=list, max_length=5)


class ChatResponse(BaseModel):
    conversation_id: UUID
    reply: str


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str | None
    created_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    role: MessageRole
    content: str


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: MemoryKind
    content: str
    importance: int
    event_at: datetime | None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str
    due_at: datetime | None
    status: TaskStatus


# ---------------------------------------------------------------- habits

HabitFrequency = Literal["daily", "weekday", "weekly"]


class HabitCreate(Strict):
    user_id: UUID
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    frequency: HabitFrequency = "daily"


class HabitEntryCreate(Strict):
    user_id: UUID
    habit_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    value: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = "done"
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""
