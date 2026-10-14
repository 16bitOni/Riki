"""Tests for the proactive nudge engine (app/nudge.py).

Uses the real database via the `user` fixture from conftest.py.
LLM calls and notify_fn are monkeypatched so tests are fast and free.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from app import nudge as nudge_module
from app.config import settings
from app.db import session_scope
from app.models import Conversation, Message, ProactiveNudge, UserReflection
from app.nudge import NudgePlan, maybe_send_nudge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _insert_reflection(user, content="## Riki's current read\n\n**Open loops:** Auth feature due Friday."):
    async with session_scope() as session:
        session.add(UserReflection(user_id=user.id, content=content, memory_count=1))


async def _insert_recent_message(user):
    """Insert a user Message created just now (requires a Conversation row)."""
    async with session_scope() as session:
        conv = Conversation(user_id=user.id, title="test")
        session.add(conv)
        await session.flush()
        session.add(Message(
            conversation_id=conv.id,
            role="user",
            content="test message",
        ))


async def _insert_recent_nudge(user):
    async with session_scope() as session:
        session.add(ProactiveNudge(
            user_id=user.id,
            content="hey did the project ship?",
            topic="project ship",
            kind="open_loop",
            sent_at=datetime.now(timezone.utc),
        ))


def _mock_plan_yes(monkeypatch):
    async def _fake_plan(user, reflection_content):
        return NudgePlan(
            should_send=True,
            message="hey did the auth feature ship by Friday?",
            topic="auth feature ship",
            kind="open_loop",
        )
    monkeypatch.setattr(nudge_module, "plan_nudge", _fake_plan)


def _mock_plan_no(monkeypatch):
    async def _fake_plan(user, reflection_content):
        return NudgePlan(should_send=False)
    monkeypatch.setattr(nudge_module, "plan_nudge", _fake_plan)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_no_nudge_when_disabled(user, monkeypatch):
    monkeypatch.setattr(settings, "nudge_enabled", False)
    await _insert_reflection(user)
    sent = []
    async def _notify(msg): sent.append(msg)
    result = await maybe_send_nudge(user, _notify)
    assert result is False
    assert not sent


async def test_no_nudge_when_no_reflection(user, monkeypatch):
    _mock_plan_yes(monkeypatch)
    sent = []
    async def _notify(msg): sent.append(msg)
    result = await maybe_send_nudge(user, _notify)
    assert result is False
    assert not sent


async def test_no_nudge_when_user_active_recently(user, monkeypatch):
    await _insert_reflection(user)
    await _insert_recent_message(user)
    _mock_plan_yes(monkeypatch)
    sent = []
    async def _notify(msg): sent.append(msg)
    result = await maybe_send_nudge(user, _notify)
    assert result is False
    assert not sent


async def test_no_nudge_too_soon(user, monkeypatch):
    await _insert_reflection(user)
    await _insert_recent_nudge(user)
    _mock_plan_yes(monkeypatch)
    sent = []
    async def _notify(msg): sent.append(msg)
    result = await maybe_send_nudge(user, _notify)
    assert result is False
    assert not sent


async def test_nudge_sent_when_plan_says_yes(user, monkeypatch):
    await _insert_reflection(user)
    _mock_plan_yes(monkeypatch)
    sent = []
    async def _notify(msg): sent.append(msg)
    result = await maybe_send_nudge(user, _notify)
    assert result is True
    assert len(sent) == 1
    assert "auth feature" in sent[0]

    # DB row created
    async with session_scope() as session:
        row = await session.scalar(
            select(ProactiveNudge).where(ProactiveNudge.user_id == user.id)
        )
    assert row is not None
    assert row.topic == "auth feature ship"
    assert row.kind == "open_loop"


async def test_nudge_skipped_when_plan_says_no(user, monkeypatch):
    await _insert_reflection(user)
    _mock_plan_no(monkeypatch)
    sent = []
    async def _notify(msg): sent.append(msg)
    result = await maybe_send_nudge(user, _notify)
    assert result is False
    assert not sent

    async with session_scope() as session:
        count = await session.scalar(
            select(ProactiveNudge).where(ProactiveNudge.user_id == user.id)
        )
    assert count is None
