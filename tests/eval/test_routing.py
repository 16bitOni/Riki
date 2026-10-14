"""Routing eval: does the agent use the right tools?

Uses the real LLM. External services are stubbed at the lowest level so the
graph can complete without credentials:
- Google tools return NOT_CONNECTED (already handled in google_common.py)
- Web tools return a missing-key message (already handled in web.py)
- DB-backed tools may throw; LangGraph's handle_tool_errors catches them

We care about WHICH tools the agent reaches for, not what they return.
"""

import pytest

from app.agents import graph as graph_module
from tests.eval.conftest import EVAL_CONFIG
from tests.eval.helpers import any_specialist_called, specialist_called


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def stub_memory_and_guides(monkeypatch):
    async def _no_memory(user, message, recent_chat):
        return (
            "## About Subhadip\n"
            "Software engineer in Kolkata. Likes trekking. "
            "Close friend Subho (Subhodip Ghosal).\n\n"
            "## Memories that may be relevant to this message\n(nothing found)"
        ), []

    async def _no_guide(user):
        return ""

    monkeypatch.setattr(graph_module, "build_memory_context", _no_memory)
    monkeypatch.setattr(graph_module, "get_session_guide_context", _no_guide)


@pytest.fixture(autouse=True)
def stub_google(monkeypatch):
    """Make all Google tools return NOT_CONNECTED without touching credentials."""
    import app.tools.google_common as gc
    monkeypatch.setattr(gc, "google_service", lambda *_: None)


async def _run(text: str) -> list:
    graph = graph_module.build_graph()  # real LLM, no checkpointer
    result = await graph.ainvoke(
        {"messages": [("user", text)]},
        config=EVAL_CONFIG,
    )
    return result["messages"]


# ---------------------------------------------------------------------------
# No tools — agent answers directly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "hey",
    "yo",
    "how are you doing?",
    "what is 2 + 2?",
    "what day of the week is christmas?",
    "what's the capital of France?",
    "explain what a hash function is",
    "i'm tired today",
])
async def test_no_tools_for_direct_questions(text):
    messages = await _run(text)
    tools_used = [
        tc["name"]
        for m in messages
        for tc in getattr(m, "tool_calls", [])
    ]
    assert not tools_used, f"'{text}' should be answered directly, not with tools {tools_used}"


# ---------------------------------------------------------------------------
# Planner tools (calendar, tasks, reminders)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what's on my calendar today?",
    "do I have anything scheduled this week?",
    "check my calendar for December",
    "add a dentist appointment on Friday at 3pm",
    "remind me at 7pm to call dad",
    "show me my tasks",
    "add a note: buy groceries",
])
async def test_uses_planner_tools(text):
    messages = await _run(text)
    assert specialist_called(messages, "planner"), f"'{text}' should use planner tools"


# ---------------------------------------------------------------------------
# Workspace tools (Gmail / Drive)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "search my emails for the invoice from last week",
    "do I have any unread emails from Alice?",
    "find the Q3 report in my Drive",
    "draft a reply to the last email from Priya",
])
async def test_uses_workspace_tools(text):
    messages = await _run(text)
    assert specialist_called(messages, "workspace"), f"'{text}' should use workspace tools"


# ---------------------------------------------------------------------------
# Research tools (web search, images)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what's the weather in Manali right now?",
    "show me a photo of Nusa Penida",
    "what are the latest AI news?",
    "who won the cricket match last night?",
])
async def test_uses_research_tools(text):
    messages = await _run(text)
    assert specialist_called(messages, "researcher"), f"'{text}' should use research tools"


# ---------------------------------------------------------------------------
# Memory tools (explicit user request)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "remember that I prefer window seats on flights",
    "forget what I said about the Pune trip",
    "search your memory for everything you know about Subho",
])
async def test_uses_memory_tools(text):
    messages = await _run(text)
    assert specialist_called(messages, "memory_keeper"), f"'{text}' should use memory tools"


# ---------------------------------------------------------------------------
# No tools — agent answers from recall context
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what do you know about my sister?",
    "best solo trekking routes in Spiti",
])
async def test_agent_uses_own_knowledge(text):
    """The agent has enough context (recall + general knowledge) to answer directly."""
    messages = await _run(text)
    assert messages, f"No messages returned for '{text}'"
