"""Response-quality eval: does the final reply follow Riki's voice rules?

Checks the actual text the user would receive for a set of inputs.
No LLM judge needed — the rules are specific enough to assert mechanically.
"""

import pytest

from app.agents import graph as graph_module
from tests.eval.conftest import EVAL_CONFIG
from tests.eval.helpers import final_text

# Phrases the prompt explicitly bans
BANNED_OPENERS = [
    "great question",
    "absolutely",
    "sure thing",
    "i'd be happy to",
    "certainly",
    "of course",
    "i'd love to",
]

BANNED_CLOSERS = [
    "let me know if",
    "feel free to",
    "anything else",
    "happy to help",
    "hope this helps",
    "is there anything",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def stub_memory_and_guides(monkeypatch):
    async def _no_memory(user, message, recent_chat):
        return (
            "## About Subhadip\n"
            "Software engineer in Kolkata. Likes trekking.\n\n"
            "## Memories that may be relevant to this message\n(nothing found)"
        ), []

    async def _no_guide(user):
        return ""

    monkeypatch.setattr(graph_module, "build_memory_context", _no_memory)
    monkeypatch.setattr(graph_module, "get_session_guide_context", _no_guide)


async def _reply(text: str) -> str:
    graph = graph_module.build_graph()
    result = await graph.ainvoke(
        {"messages": [("user", text)]},
        config=EVAL_CONFIG,
    )
    return final_text(result["messages"])


# ---------------------------------------------------------------------------
# Banned openers / closers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what is photosynthesis?",
    "explain recursion",
    "i had a great day today",
    "should i learn rust or go?",
])
async def test_no_banned_openers(text):
    reply = await _reply(text)
    lower = reply.lower()
    for phrase in BANNED_OPENERS:
        assert phrase not in lower, (
            f"Reply to '{text}' started with banned phrase '{phrase}':\n{reply}"
        )


@pytest.mark.parametrize("text", [
    "what is the best programming language?",
    "give me advice on time management",
    "i'm thinking of changing jobs",
])
async def test_no_banned_closers(text):
    reply = await _reply(text)
    # Only check the last 120 chars — banned closers are trailing offers, not mid-sentence content.
    tail = reply.lower()[-120:]
    for phrase in BANNED_CLOSERS:
        assert phrase not in tail, (
            f"Reply to '{text}' ended with banned phrase '{phrase}':\n...{reply[-200:]}"
        )


# ---------------------------------------------------------------------------
# Brevity for greetings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["hey", "hi", "yo", "sup"])
async def test_greeting_is_short(text):
    reply = await _reply(text)
    words = reply.split()
    assert len(words) <= 10, (
        f"Greeting reply should be ≤10 words, got {len(words)}:\n{reply}"
    )


# ---------------------------------------------------------------------------
# Opinionated answers (no hedge stacks)
# ---------------------------------------------------------------------------

HEDGE_PATTERNS = [
    "it depends",
    "both have merits",
    "a few ways to think",
    "there are pros and cons",
    "on one hand",
    "on the other hand",
]


@pytest.mark.parametrize("text", [
    "should i use postgres or mysql for my next project?",
    "is react or vue better?",
])
async def test_gives_opinion_not_hedge(text):
    reply = await _reply(text)
    lower = reply.lower()
    hedges_found = [p for p in HEDGE_PATTERNS if p in lower]
    assert not hedges_found, (
        f"Reply to '{text}' hedged instead of taking a position:\n{reply}"
    )


# ---------------------------------------------------------------------------
# Calendar date range: planner should use start_date/end_date not days_ahead
# ---------------------------------------------------------------------------

async def test_calendar_december_uses_date_range(monkeypatch):
    """When asked about 'December', the planner should call list_calendar_events
    with explicit start_date/end_date, not days_ahead."""
    from tests.eval.helpers import tool_args

    captured: list[dict] = []

    # Intercept list_calendar_events calls to inspect arguments
    original = graph_module  # just to keep reference

    import app.tools.calendar as cal

    _orig_list = cal.list_calendar_events

    async def _capture_list(**kwargs):
        config = kwargs.pop("config", None)
        captured.append(dict(kwargs))
        return "- Baba's birthday: 2026-12-05\n- Tukai didi: 2026-12-09\n- Subhodip: 2026-12-27"

    # Patch the tool's coroutine
    monkeypatch.setattr(cal.list_calendar_events, "coroutine", _capture_list, raising=False)

    # Simpler: patch google_service to avoid real calls
    import app.tools.google_common as gc
    monkeypatch.setattr(gc, "google_service", lambda *_: "NOT_CONNECTED")

    async def _no_memory(user, message, recent_chat):
        return "## About Subhadip\n(nothing)\n\n## Memories\n(nothing found)", []

    async def _no_guide(user):
        return ""

    monkeypatch.setattr(graph_module, "build_memory_context", _no_memory)
    monkeypatch.setattr(graph_module, "get_session_guide_context", _no_guide)

    graph = graph_module.build_graph()
    result = await graph.ainvoke(
        {"messages": [("user", "what's on my calendar in December?")]},
        config=EVAL_CONFIG,
    )

    messages = result["messages"]
    # Check that list_calendar_events was called with start_date covering December
    args = tool_args(messages, "list_calendar_events")
    if args:  # tool was called
        assert "start_date" in args, (
            f"list_calendar_events should use start_date for December, got args: {args}"
        )
        assert args["start_date"].startswith("2026-12"), (
            f"start_date should be in December, got: {args['start_date']}"
        )
