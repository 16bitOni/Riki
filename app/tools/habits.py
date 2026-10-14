"""Habit tracking tools for the agent."""

from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.features.habits import (
    deactivate_habit,
    get_or_create_habit,
    list_habits,
    log_entry,
    recent_entries,
    unlogged_today,
)


@tool
async def track_habit(
    habit_name: str,
    value: str = "done",
    note: str = "",
    frequency: str = "daily",
    *,
    config: RunnableConfig,
) -> str:
    """Log a habit entry. Creates the habit automatically if it doesn't exist yet.

    Examples:
      - "I ran 5k" → habit_name="running", value="5k"
      - "meditated for 20 mins" → habit_name="meditation", value="20 mins"
      - "slept 7 hours" → habit_name="sleep", value="7 hours"
      - "I skipped gym today" → habit_name="gym", value="skipped"

    frequency: "daily" (default), "weekday" (Mon-Fri), or "weekly"
    """
    user = config["configurable"]["user"]
    entry = await log_entry(user.id, habit_name, value, note)
    return f"Logged {habit_name}: {value}" + (f" ({note})" if note else "")


@tool
async def show_habits(days: int = 7, *, config: RunnableConfig) -> str:
    """Show all active habits with recent entries and streaks."""
    user = config["configurable"]["user"]
    habits = await list_habits(user.id)
    if not habits:
        return "No habits being tracked yet."

    lines = []
    for h in habits:
        entries = await recent_entries(user.id, h.id, days)
        streak = len(entries)
        recent = ", ".join(f"{e.value}" for e in entries[:3])
        line = f"- **{h.name}** ({h.frequency}): {streak} entries in {days}d"
        if recent:
            line += f" — recent: {recent}"
        lines.append(line)

    missing = await unlogged_today(user.id, user.timezone)
    if missing:
        lines.append(f"\nNot logged today: {', '.join(h.name for h in missing)}")

    return "\n".join(lines)


@tool
async def stop_tracking_habit(habit_name: str, *, config: RunnableConfig) -> str:
    """Stop tracking a habit. The history is kept but it won't appear in daily check-ins."""
    user = config["configurable"]["user"]
    if await deactivate_habit(user.id, habit_name):
        return f"Stopped tracking '{habit_name}'. History is kept."
    return f"No active habit called '{habit_name}' found."
