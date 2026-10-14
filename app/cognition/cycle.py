"""Cognition v2: the agent thinking between conversations.

Four-phase pipeline per cycle:
  Phase 1 — Compute  (pure SQL signals, no LLM)
  Phase 2 — Think    (LLM interprets signals, updates threads and state)
  Phase 3 — Update   (persist state, threads, reflection — no LLM)
  Phase 4 — Act      (separate conservative LLM call: should I reach out?)

The LLM interprets; SQL counts. Thinking and acting are separate calls.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.cognition.attention import (
    ThreadAssessment,
    abandon_stale_threads,
    add_evidence,
    create_thread,
    format_threads_for_prompt,
    get_active_threads,
    get_thread_accuracy_stats,
    increment_stale_counters,
    resolve_thread,
)
from app.config import settings
from app.core.db import session_scope
from app.core.llm import chat_model
from app.core.models import (
    CognitionCycle,
    Conversation,
    Memory,
    Message,
    ProactiveNudge,
    SessionGuide,
    UserReflection,
)
from app.obs import trace as obs
from app.cognition.reflection import get_latest_reflection
from app.cognition.signals import compute_all_signals
from app.cognition.state import (
    UserDimensions,
    format_diff_for_prompt,
    format_state_for_prompt,
    get_latest_state,
    save_state,
)
from app.core.timeutil import now_in

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- LLM output models

class StateInterpretation(BaseModel):
    mood_value: str = Field(default="neutral", description="neutral, stressed, energized, low, anxious, content, etc.")
    mood_confidence: float = Field(default=0.5, ge=0, le=1)
    mood_trend: str = Field(default="stable", description="improving, stable, declining")
    mood_since: str | None = Field(default=None, description="ISO date when this mood started, if known")
    energy_level: str = Field(default="moderate", description="high, moderate, low")
    energy_trend: str = Field(default="stable", description="improving, stable, declining")
    active_focuses: list[dict] = Field(default_factory=list, description="[{topic, intensity, mention_count}] — what they're working on")
    open_loops: list[dict] = Field(default_factory=list, description="[{topic, age_days, last_evidence}] — unresolved things")


class ThinkOutput(BaseModel):
    monologue: str = Field(description="Private inner thoughts, 100-200 words, first person, builds on prior monologue")
    thread_assessments: list[ThreadAssessment] = Field(default_factory=list)
    state_interpretation: StateInterpretation = Field(default_factory=StateInterpretation)
    updated_reflection: str = Field(description="Updated reflection document in the standard format")


class ActOutput(BaseModel):
    should_reach_out: bool = Field(default=False)
    reach_out_message: str = Field(default="")
    reach_out_topic: str = Field(default="")
    reach_out_kind: Literal["open_loop", "pattern", "milestone", "deadline", "curiosity"] = "open_loop"
    linked_thread_id: str = Field(default="", description="UUID of the attention thread that prompted this, if any")


# ---------------------------------------------------------------- prompts

THINK_PROMPT = """You are Riki, thinking between conversations with {name}. It's {current_time}.

Below is structured data about {name} — computed from the database, not guessed.
Your job is to INTERPRET this data, build on your prior thoughts, update your understanding,
and track what you're watching.

{prior_state_section}

## Your recent inner monologue
{prior_monologue}

## Computed signals (this cycle)

{signals_section}

## Active attention threads
{threads_section}

## New memories since last cycle ({memory_count} total)
{new_memories}

## Recent session learnings
{guide_text}

---

Produce:

1. **Monologue** (100-200 words, first person): Think out loud. Build on your prior monologue.
   Reference threads by topic. Notice what the signals are telling you that the user hasn't
   explicitly said. E.g.: "Last cycle I was watching the auth feature — now I see 8 mentions
   in 14 days (3x his normal rate) but no resolution memory. Still stuck."
   This is private — nobody reads it but you.

2. **Thread assessments**: For each active thread, decide:
   - add_evidence: new data supports or challenges the hypothesis
   - resolve: the thread is clearly resolved (hypothesis confirmed or clearly wrong)
   - no_change: nothing new to add
   Also create new threads for patterns you're now actively watching.
   Keep threads focused and specific. Don't create a thread for everything.

3. **State interpretation**: Based on signals + memories + thread status, what do you
   currently believe about {name}'s mood, energy, active focus, and open loops?
   Be specific. "stressed" needs a reason. Confidence reflects how sure you are.

4. **Updated reflection**: Rewrite your reflection (same format: Right now / Open loops /
   Questions I'm sitting with / Patterns). This is what gets injected into future conversations.
   Under 450 words. Be specific — use real names, dates, observations from the data."""


ACT_PROMPT = """You are Riki. You've just finished thinking about {name}. It's {current_time}.

Now decide: should you reach out right now?

## Current state
{state_summary}

## Active attention threads with triggers
{triggered_threads}

## Recent nudges sent (DO NOT repeat these topics)
{recent_nudges}

## Your reach-out track record
{accuracy_stats}

Rules:
- Most cycles the answer is NO. Reach out only if a specific trigger condition is met OR
  something genuinely time-sensitive needs attention.
- Never reach out during quiet hours (handled externally), never repeat recent topics.
- If you reach out, write 1-2 sentences. Casual, specific, like a friend who noticed something.
  Not a bot, not a check-in, not generic.

Bad reach-outs: "How are you doing?", "Just checking in!", repeating anything recent.
Good reach-outs: "The auth feature's been on your mind for days — you said 'tomorrow' twice.
  Did you ship it or hit a wall?" or "Tax docs are 3 days overdue. Still on your list?"

Set should_reach_out=False if: nothing specific, wrong time, covered recently, or it can wait."""


# ---------------------------------------------------------------- helpers

async def _get_memories_since(user_id: UUID, since: datetime) -> list[Memory]:
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(Memory)
                .where(Memory.user_id == user_id, Memory.is_active.is_(True), Memory.created_at > since)
                .order_by(Memory.created_at.desc())
                .limit(100)
            )
        ).all()
    return list(rows)


async def _get_recent_conversations(user_id: UUID, since: datetime, limit: int = 5) -> str:
    async with session_scope() as session:
        convos = (
            await session.scalars(
                select(Conversation)
                .where(Conversation.user_id == user_id, Conversation.created_at > since)
                .order_by(Conversation.created_at.desc())
                .limit(limit)
            )
        ).all()
    if not convos:
        return "(no conversations since last cycle)"
    lines = []
    for c in convos:
        age = datetime.now(timezone.utc) - c.created_at
        ago = f"{age.days}d ago" if age.days else f"{age.seconds // 3600}h ago"
        lines.append(f"- {ago}: {c.title or '(untitled)'}")
        async with session_scope() as session:
            msgs = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == c.id, Message.role == "user")
                    .order_by(Message.created_at.desc())
                    .limit(3)
                )
            ).all()
        for m in reversed(msgs):
            lines.append(f"    user: {m.content[:150]}")
    return "\n".join(lines)


async def _get_recent_nudges(user_id: UUID, hours: int = 168) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(ProactiveNudge)
                .where(ProactiveNudge.user_id == user_id, ProactiveNudge.sent_at >= cutoff)
                .order_by(ProactiveNudge.sent_at.desc())
            )
        ).all()
    if not rows:
        return "(none)"
    return "\n".join(f"- [{n.kind}] {n.topic}: {n.content[:100]}" for n in rows)


async def _get_recent_nudges_raw(user_id: UUID, hours: int) -> list[ProactiveNudge]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(ProactiveNudge)
                .where(ProactiveNudge.user_id == user_id, ProactiveNudge.sent_at >= cutoff)
                .order_by(ProactiveNudge.sent_at.desc())
            )
        ).all()
    return list(rows)


async def _get_recent_guides(user_id: UUID, limit: int = 2) -> str:
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(SessionGuide)
                .where(SessionGuide.user_id == user_id)
                .order_by(SessionGuide.created_at.desc())
                .limit(limit)
            )
        ).all()
    lines = [g.learnings for g in reversed(rows) if g.learnings and g.learnings != "(no learnings)"]
    return "\n---\n".join(lines) if lines else ""


async def _get_recent_monologues(user_id: UUID, limit: int = 3) -> str:
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(CognitionCycle)
                .where(CognitionCycle.user_id == user_id, CognitionCycle.monologue != "")
                .order_by(CognitionCycle.created_at.desc())
                .limit(limit)
            )
        ).all()
    if not rows:
        return "(this is your first time thinking — no prior thoughts yet)"
    entries = []
    for cycle in reversed(rows):
        when = cycle.created_at.strftime("%Y-%m-%d %H:%M")
        entries.append(f"[{when}] {cycle.monologue}")
    return "\n\n".join(entries)


async def _get_last_cycle(user_id: UUID) -> CognitionCycle | None:
    async with session_scope() as session:
        return await session.scalar(
            select(CognitionCycle)
            .where(CognitionCycle.user_id == user_id)
            .order_by(CognitionCycle.created_at.desc())
            .limit(1)
        )


def _in_quiet_hours(tz: str) -> bool:
    local_hour = now_in(tz).hour
    start = settings.cognition_quiet_hours_start
    end = settings.cognition_quiet_hours_end
    if start > end:
        return local_hour >= start or local_hour < end
    return start <= local_hour < end


def _format_signals(signals: dict) -> str:
    """Render computed signals as readable structured blocks for the LLM."""
    parts = []

    ef = signals.get("entity_frequency", [])
    if ef:
        lines = ["### Entity activity"]
        for e in sorted(ef, key=lambda x: x.get("recent_count", 0), reverse=True)[:10]:
            spike = " ⚡SPIKE" if e.get("is_spike") else ""
            lines.append(f"- \"{e['name']}\": {e['recent_count']} mentions (ratio {e.get('ratio', 0):.1f}x baseline){spike}")
        parts.append("\n".join(lines))

    er = signals.get("entity_recency", [])
    fading = [e for e in er if e.get("status") == "fading"]
    resurfaced = [e for e in er if e.get("status") == "resurfaced"]
    if fading or resurfaced:
        lines = ["### Entity status changes"]
        for e in resurfaced:
            lines.append(f"- \"{e['name']}\": RESURFACED (last seen {e.get('days_since_last', '?')}d ago)")
        for e in fading[:5]:
            lines.append(f"- \"{e['name']}\": FADING (last seen {e.get('days_since_last', '?')}d ago, avg gap {e.get('avg_gap_days', '?'):.0f}d)")
        parts.append("\n".join(lines))

    hs = signals.get("habit_streaks", [])
    if hs:
        lines = ["### Habit streaks"]
        for h in hs:
            lines.append(f"- {h['habit_name']}: {h['current_streak']} day streak (last: {h.get('last_logged', 'never')})")
        parts.append("\n".join(lines))

    oc = signals.get("open_commitments", {})
    if oc:
        lines = ["### Open commitments"]
        lines.append(f"- {oc.get('open_tasks', 0)} open tasks, {oc.get('overdue_tasks', 0)} overdue")
        for t in oc.get("due_soon", []):
            lines.append(f"  - \"{t['title']}\" due in {t.get('days_until', '?')}d")
        if oc.get("pending_reminders", 0):
            lines.append(f"- {oc['pending_reminders']} pending reminders")
        parts.append("\n".join(lines))

    cc = signals.get("conversation_cadence", {})
    if cc:
        quiet = " QUIET" if cc.get("is_quiet") else ""
        parts.append(f"### Conversation cadence\n- Last conversation: {cc.get('hours_since_last', '?'):.0f}h ago (avg: {cc.get('avg_gap_hours', '?'):.0f}h){quiet}")

    ap = signals.get("activity_pattern", {})
    if ap.get("shift_detected"):
        parts.append("### Activity pattern\n- ⚡ Unusual activity pattern detected this week vs baseline")

    return "\n\n".join(parts) if parts else "(no signal data)"


# ---------------------------------------------------------------- main cycle

async def run_cognition_cycle(user, notify_fn=None) -> CognitionCycle | None:
    """Run one cognition cycle: compute → think → update → act.

    Returns the CognitionCycle record, or None if skipped.
    """
    t0 = time.monotonic()
    phase_times: dict[str, int] = {}
    trace = obs.start("cognition", "background", "thinking between conversations")

    try:
        # ---- Phase 1: Compute (pure SQL)
        tp1 = time.monotonic()
        last_cycle = await _get_last_cycle(user.id)
        since = last_cycle.created_at if last_cycle else datetime.now(timezone.utc) - timedelta(hours=24)

        new_memories = await _get_memories_since(user.id, since)

        if settings.cognition_min_new_memories > 0 and len(new_memories) < settings.cognition_min_new_memories:
            log.info("Cognition skipped — only %d new memories (need %d)", len(new_memories), settings.cognition_min_new_memories)
            if trace:
                trace.meta.update(skipped=True, reason="too_few_memories")
            await obs.finish(trace)
            return None

        signals = await compute_all_signals(user.id, user.timezone)
        active_threads = await get_active_threads(user.id)
        latest_state = await get_latest_state(user.id)
        prior_monologue = await _get_recent_monologues(user.id)
        guide_text = await _get_recent_guides(user.id)
        recent_nudges_str = await _get_recent_nudges(user.id)

        phase_times["compute"] = int((time.monotonic() - tp1) * 1000)
        log.debug("Phase 1 (compute): %dms, %d memories, %d threads", phase_times["compute"], len(new_memories), len(active_threads))

        # ---- Phase 2: Think (main LLM call)
        tp2 = time.monotonic()
        from app.memory.recall import format_memories
        current_time = now_in(user.timezone).strftime("%A %Y-%m-%d %I:%M %p %Z")

        prior_state_section = (
            f"## Your prior state\n{format_state_for_prompt(latest_state)}\n\n"
            f"## What changed since last cycle\n{format_diff_for_prompt(latest_state.diff_from_previous if latest_state else None)}"
        )

        think_llm = chat_model(purpose="cognition_think").with_structured_output(ThinkOutput)
        think_result: ThinkOutput = await think_llm.ainvoke([
            SystemMessage(THINK_PROMPT.format(
                name=user.name,
                current_time=current_time,
                prior_state_section=prior_state_section,
                prior_monologue=prior_monologue,
                signals_section=_format_signals(signals),
                threads_section=format_threads_for_prompt(active_threads),
                memory_count=len(new_memories),
                new_memories=format_memories(new_memories) if new_memories else "(nothing new)",
                guide_text=guide_text or "(none)",
            )),
            HumanMessage("Think through what's changed and update your understanding."),
        ])
        phase_times["think"] = int((time.monotonic() - tp2) * 1000)
        log.debug("Phase 2 (think): %dms, %d thread assessments", phase_times["think"], len(think_result.thread_assessments))

        # ---- Phase 3: Update (DB writes, no LLM)
        tp3 = time.monotonic()

        # Save updated reflection
        async with session_scope() as session:
            total_memories = await session.scalar(
                select(func.count()).select_from(Memory).where(
                    Memory.user_id == user.id, Memory.is_active.is_(True)
                )
            ) or 0
            reflection = UserReflection(
                user_id=user.id,
                content=think_result.updated_reflection,
                memory_count=total_memories,
            )
            session.add(reflection)
            await session.flush()
            reflection_id = reflection.id

        # Process thread assessments
        active_thread_ids = {str(t.id) for t in active_threads}
        updated_thread_ids: set[UUID] = set()

        for assessment in think_result.thread_assessments:
            if assessment.action == "create":
                new_thread = await create_thread(
                    user_id=user.id,
                    topic=assessment.topic,
                    hypothesis=assessment.hypothesis,
                    trigger=assessment.trigger,
                    confidence=assessment.confidence,
                )
                if new_thread:
                    updated_thread_ids.add(new_thread.id)

            elif assessment.action == "add_evidence" and assessment.thread_id in active_thread_ids:
                try:
                    tid = UUID(assessment.thread_id)
                    updated = await add_evidence(tid, reflection_id, assessment.observation)
                    if updated:
                        updated_thread_ids.add(tid)
                except (ValueError, Exception):
                    pass

            elif assessment.action == "resolve" and assessment.thread_id in active_thread_ids:
                try:
                    tid = UUID(assessment.thread_id)
                    await resolve_thread(tid, assessment.resolution, assessment.was_accurate)
                    updated_thread_ids.add(tid)
                except (ValueError, Exception):
                    pass

        await increment_stale_counters(user.id, updated_thread_ids)
        abandoned = await abandon_stale_threads(user.id)
        if abandoned:
            log.info("Auto-abandoned %d stale threads", len(abandoned))

        # Build and save user state
        si = think_result.state_interpretation
        entity_spotlight = []
        er = signals.get("entity_recency", [])
        for e in er:
            if e.get("status") in ("resurfaced", "active") and e.get("total_mentions", 0) >= 2:
                entity_spotlight.append({
                    "name": e["name"],
                    "status": e["status"],
                    "last_seen": e.get("last_seen", ""),
                    "frequency": round(e.get("total_mentions", 0) / max(30, 1) * 7, 1),
                })

        habits_state = {}
        for hs in signals.get("habit_streaks", []):
            streak = hs.get("current_streak", 0)
            habits_state[hs["habit_name"]] = {
                "streak": streak,
                "trend": "building" if streak > 3 else ("slipping" if streak == 0 else "stable"),
                "last_logged": hs.get("last_logged"),
            }

        dimensions = {
            "mood": {
                "value": si.mood_value,
                "confidence": si.mood_confidence,
                "trend": si.mood_trend,
                "since": si.mood_since,
                "evidence_ids": [],
            },
            "energy": {"level": si.energy_level, "trend": si.energy_trend},
            "active_focus": si.active_focuses,
            "entity_spotlight": entity_spotlight[:8],
            "habits": habits_state,
            "open_loops": si.open_loops,
        }

        new_state = await save_state(user.id, dimensions, signals, latest_state)

        phase_times["update"] = int((time.monotonic() - tp3) * 1000)
        log.debug("Phase 3 (update): %dms", phase_times["update"])

        # ---- Phase 4: Act (fast model, conservative)
        tp4 = time.monotonic()
        accuracy_stats = await get_thread_accuracy_stats(user.id)
        accuracy_str = (
            f"Thread accuracy: {accuracy_stats['accurate']}/{accuracy_stats['total_resolved']} confirmed"
            if accuracy_stats["total_resolved"] > 0 else "No track record yet"
        )

        threads_with_triggers = [t for t in active_threads if t.trigger and t.trigger.strip()]
        triggered_text = format_threads_for_prompt(threads_with_triggers) if threads_with_triggers else "(no threads with triggers)"

        act_llm = chat_model(fast=True, purpose="cognition_act").with_structured_output(ActOutput)
        act_result: ActOutput = await act_llm.ainvoke([
            SystemMessage(ACT_PROMPT.format(
                name=user.name,
                current_time=current_time,
                state_summary=format_state_for_prompt(new_state),
                triggered_threads=triggered_text,
                recent_nudges=recent_nudges_str,
                accuracy_stats=accuracy_str,
            )),
            HumanMessage("Should you reach out right now?"),
        ])
        phase_times["act"] = int((time.monotonic() - tp4) * 1000)
        log.debug("Phase 4 (act): %dms, should_reach_out=%s", phase_times["act"], act_result.should_reach_out)

        # ---- Send reach-out if warranted
        reached_out = False
        reach_out_nudge_id = None

        if act_result.should_reach_out and act_result.reach_out_message and notify_fn and not _in_quiet_hours(user.timezone):
            recent_nudge_check = await _get_recent_nudges_raw(user.id, hours=settings.nudge_min_interval_hours)
            if not recent_nudge_check:
                telegram_msg_id = await notify_fn(act_result.reach_out_message)
                linked_thread_id = None
                if act_result.linked_thread_id:
                    try:
                        linked_thread_id = UUID(act_result.linked_thread_id)
                    except ValueError:
                        pass
                async with session_scope() as session:
                    nudge = ProactiveNudge(
                        user_id=user.id,
                        content=act_result.reach_out_message,
                        topic=act_result.reach_out_topic,
                        kind=act_result.reach_out_kind,
                        telegram_message_id=telegram_msg_id,
                        attention_thread_id=linked_thread_id,
                    )
                    session.add(nudge)
                    await session.flush()
                    reach_out_nudge_id = nudge.id
                reached_out = True
                log.info("Cognition reach-out sent: topic=%r thread=%s", act_result.reach_out_topic, act_result.linked_thread_id or "none")
            else:
                log.info("Cognition reach-out suppressed — recent nudge within %dh", settings.nudge_min_interval_hours)

        # ---- Save cycle record
        duration_ms = int((time.monotonic() - t0) * 1000)
        signals_summary = {
            "entity_spikes": [e["name"] for e in signals.get("entity_frequency", []) if e.get("is_spike")],
            "quiet": signals.get("conversation_cadence", {}).get("is_quiet", False),
            "overdue_tasks": signals.get("open_commitments", {}).get("overdue_tasks", 0),
            "habit_streaks": {h["habit_name"]: h["current_streak"] for h in signals.get("habit_streaks", [])},
        }

        async with session_scope() as session:
            cycle = CognitionCycle(
                user_id=user.id,
                reflection_id=reflection_id,
                memories_reviewed=len(new_memories),
                notices=[],
                monologue=think_result.monologue,
                reached_out=reached_out,
                reach_out_message=act_result.reach_out_message if reached_out else None,
                reach_out_topic=act_result.reach_out_topic if reached_out else None,
                duration_ms=duration_ms,
                phase_durations=phase_times,
                signals_summary=signals_summary,
            )
            session.add(cycle)

        log.info(
            "Cognition cycle complete  memories=%d  threads=%d  reached_out=%s  %dms  phases=%s",
            len(new_memories), len(active_threads), reached_out, duration_ms,
            {k: f"{v}ms" for k, v in phase_times.items()},
        )

        if trace:
            trace.meta.update(
                memories_reviewed=len(new_memories),
                threads_active=len(active_threads),
                threads_updated=len(updated_thread_ids),
                threads_abandoned=len(abandoned),
                reached_out=reached_out,
                duration_ms=duration_ms,
                phase_durations=phase_times,
            )
            trace.reply_text = think_result.updated_reflection[:500]

        return cycle

    except Exception as error:
        log.exception("Cognition cycle failed")
        if trace:
            trace.fail(error)
        return None
    finally:
        await obs.finish(trace)
