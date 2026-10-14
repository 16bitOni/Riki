"""User state model: structured, queryable, diffable.

Each cognition cycle computes a new state from signals + LLM interpretation.
The state is JSONB with a fixed schema (validated by Pydantic before write),
so it can be diffed across cycles to show what changed.
"""

from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.db import session_scope
from app.core.models import UserState


# ---------------------------------------------------------------- dimension schemas


class MoodDimension(BaseModel):
    value: str = "neutral"
    confidence: float = 0.5
    trend: str = "stable"
    since: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class EnergyDimension(BaseModel):
    level: str = "moderate"
    trend: str = "stable"


class ActiveFocus(BaseModel):
    topic: str
    intensity: str = "moderate"
    since: str | None = None
    mention_count: int = 0


class EntitySpotlight(BaseModel):
    name: str
    status: str = "active"
    last_seen: str = ""
    frequency: float = 0.0


class HabitState(BaseModel):
    streak: int = 0
    trend: str = "stable"
    last_logged: str | None = None


class OpenLoop(BaseModel):
    topic: str
    age_days: int = 0
    last_evidence: str | None = None


class UserDimensions(BaseModel):
    mood: MoodDimension = Field(default_factory=MoodDimension)
    energy: EnergyDimension = Field(default_factory=EnergyDimension)
    active_focus: list[ActiveFocus] = Field(default_factory=list)
    entity_spotlight: list[EntitySpotlight] = Field(default_factory=list)
    habits: dict[str, HabitState] = Field(default_factory=dict)
    open_loops: list[OpenLoop] = Field(default_factory=list)


# ---------------------------------------------------------------- queries


async def get_latest_state(user_id: UUID) -> UserState | None:
    async with session_scope() as session:
        return await session.scalar(
            select(UserState)
            .where(UserState.user_id == user_id)
            .order_by(UserState.computed_at.desc())
            .limit(1)
        )


# ---------------------------------------------------------------- diff


def _topics(items: list[dict], key: str = "topic") -> set[str]:
    return {item.get(key, "") for item in items}


def compute_diff(previous: dict | None, current: dict) -> dict:
    """Compare two dimensions dicts. previous=None means first cycle."""
    diff: dict = {}
    cur = UserDimensions.model_validate(current)

    if previous is None:
        diff["first_cycle"] = True
        diff["mood_now"] = cur.mood.value
        diff["energy_now"] = cur.energy.level
        diff["new_focuses"] = [f.topic for f in cur.active_focus]
        diff["new_open_loops"] = [o.topic for o in cur.open_loops]
        return diff

    prev = UserDimensions.model_validate(previous)

    if prev.mood.value != cur.mood.value:
        diff["mood_changed"] = True
        diff["mood_was"] = prev.mood.value
        diff["mood_now"] = cur.mood.value

    if prev.energy.level != cur.energy.level:
        diff["energy_changed"] = True
        diff["energy_was"] = prev.energy.level
        diff["energy_now"] = cur.energy.level

    prev_topics = _topics([f.model_dump() for f in prev.active_focus])
    cur_topics = _topics([f.model_dump() for f in cur.active_focus])
    new_focuses = cur_topics - prev_topics
    dropped_focuses = prev_topics - cur_topics
    if new_focuses:
        diff["new_focuses"] = sorted(new_focuses)
    if dropped_focuses:
        diff["dropped_focuses"] = sorted(dropped_focuses)

    prev_entities = {e.name: e.status for e in prev.entity_spotlight}
    cur_entities = {e.name: e.status for e in cur.entity_spotlight}
    entity_changes = []
    for name, cur_status in cur_entities.items():
        prev_status = prev_entities.get(name)
        if prev_status != cur_status:
            entity_changes.append({"name": name, "was": prev_status or "unseen", "now": cur_status})
    if entity_changes:
        diff["entity_changes"] = entity_changes

    habit_changes = []
    for name, cur_h in cur.habits.items():
        prev_h = prev.habits.get(name)
        if prev_h is None or prev_h.streak != cur_h.streak:
            habit_changes.append({
                "name": name,
                "streak_was": prev_h.streak if prev_h else 0,
                "streak_now": cur_h.streak,
            })
    if habit_changes:
        diff["habit_changes"] = habit_changes

    prev_loops = _topics([o.model_dump() for o in prev.open_loops])
    cur_loops = _topics([o.model_dump() for o in cur.open_loops])
    new_loops = cur_loops - prev_loops
    resolved_loops = prev_loops - cur_loops
    if new_loops:
        diff["new_open_loops"] = sorted(new_loops)
    if resolved_loops:
        diff["resolved_open_loops"] = sorted(resolved_loops)

    return diff


# ---------------------------------------------------------------- save


async def save_state(
    user_id: UUID,
    dimensions: dict,
    signals_snapshot: dict,
    previous_state: UserState | None,
) -> UserState:
    validated = UserDimensions.model_validate(dimensions)
    dims = validated.model_dump()
    prev_dims = previous_state.dimensions if previous_state else None
    diff = compute_diff(prev_dims, dims)

    async with session_scope() as session:
        state = UserState(
            user_id=user_id,
            dimensions=dims,
            signals_snapshot=signals_snapshot,
            diff_from_previous=diff,
        )
        session.add(state)
        await session.flush()
        return state


# ---------------------------------------------------------------- prompt formatting


def format_state_for_prompt(state: UserState | None) -> str:
    if state is None:
        return "(no prior state — first cognition cycle)"
    dims = UserDimensions.model_validate(state.dimensions)
    parts = []

    mood = dims.mood
    mood_str = f"Mood: {mood.value} ({mood.trend}"
    if mood.since:
        mood_str += f", since {mood.since}"
    mood_str += f", confidence: {mood.confidence})"
    parts.append(mood_str)

    parts.append(f"Energy: {dims.energy.level} ({dims.energy.trend})")

    if dims.active_focus:
        focuses = ", ".join(
            f"{f.topic} ({f.intensity}, {f.mention_count} mentions)" for f in dims.active_focus
        )
        parts.append(f"Active focus: {focuses}")

    if dims.habits:
        habits = ", ".join(
            f"{name} ({h.streak}-day streak, {h.trend})" for name, h in dims.habits.items()
        )
        parts.append(f"Habits: {habits}")

    if dims.open_loops:
        loops = ", ".join(
            f"{o.topic} ({o.age_days}d, {o.last_evidence or 'no recent evidence'})"
            for o in dims.open_loops
        )
        parts.append(f"Open loops: {loops}")

    if dims.entity_spotlight:
        entities = ", ".join(
            f"{e.name} ({e.status})" for e in dims.entity_spotlight[:5]
        )
        parts.append(f"Entities: {entities}")

    return ". ".join(parts) + "."


def format_diff_for_prompt(diff: dict | None) -> str:
    if not diff:
        return "(first cycle — no prior state to compare)"
    if diff.get("first_cycle"):
        return "(first cycle — no prior state to compare)"

    lines = []
    if diff.get("mood_changed"):
        lines.append(f"Mood shifted from {diff['mood_was']} → {diff['mood_now']}")
    if diff.get("energy_changed"):
        lines.append(f"Energy shifted from {diff['energy_was']} → {diff['energy_now']}")
    for topic in diff.get("new_focuses", []):
        lines.append(f"New focus: {topic}")
    for topic in diff.get("dropped_focuses", []):
        lines.append(f"Dropped focus: {topic}")
    for ec in diff.get("entity_changes", []):
        lines.append(f"Entity {ec['name']}: {ec['was']} → {ec['now']}")
    for hc in diff.get("habit_changes", []):
        lines.append(f"Habit {hc['name']}: streak {hc['streak_was']} → {hc['streak_now']}")
    for topic in diff.get("new_open_loops", []):
        lines.append(f"New open loop: {topic}")
    for topic in diff.get("resolved_open_loops", []):
        lines.append(f"Resolved: {topic}")

    return ". ".join(lines) + "." if lines else "(nothing changed)"
