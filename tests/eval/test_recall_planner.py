"""Recall-planner eval: does make_plan set needs_memory correctly?

This is the cheapest eval to run — it makes one fast-model call per case
and checks the structured output. No graph invocation needed.
"""

import pytest

from app.memory.recall import make_plan


# ---------------------------------------------------------------------------
# needs_memory = False  (no personal context needed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "hey",
    "hi",
    "thanks",
    "ok",
    "what is the capital of Japan?",
    "explain how merge sort works",
    "what's 15% of 340?",
    "tell me a joke",
])
async def test_needs_memory_false_for_generic(text):
    plan = await make_plan(text, recent_chat="", timezone="Asia/Kolkata")
    assert not plan.needs_memory, (
        f"'{text}' should not need memory (got needs_memory=True, "
        f"keywords={plan.keywords})"
    )


# ---------------------------------------------------------------------------
# needs_memory = True  (personal context is useful)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what did I tell you about my sister?",
    "when is Subho's birthday?",
    "what are my travel plans for December?",
    "do you remember what I said about the Pune trip?",
    "what's my dad's phone number?",
    "what gym do I go to?",
    "what did we discuss last time about my job?",
])
async def test_needs_memory_true_for_personal(text):
    plan = await make_plan(text, recent_chat="", timezone="Asia/Kolkata")
    assert plan.needs_memory, (
        f"'{text}' should need memory (got needs_memory=False)"
    )


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

async def test_entities_extracted_for_person_query():
    plan = await make_plan(
        "what do you know about Subhodip Ghosal?",
        recent_chat="",
        timezone="Asia/Kolkata",
    )
    assert plan.needs_memory
    names = [e.lower() for e in plan.entities]
    assert any("subhodip" in n or "ghosal" in n for n in names), (
        f"Expected 'Subhodip' or 'Ghosal' in entities, got {plan.entities}"
    )


async def test_keywords_populated_for_specific_query():
    plan = await make_plan(
        "what's the address of my dentist?",
        recent_chat="",
        timezone="Asia/Kolkata",
    )
    assert plan.needs_memory
    assert len(plan.keywords) > 0, "Expected keywords for a specific query"


# ---------------------------------------------------------------------------
# Date range fields
# ---------------------------------------------------------------------------

async def test_date_range_set_for_time_scoped_query():
    plan = await make_plan(
        "what did I talk about last week?",
        recent_chat="",
        timezone="Asia/Kolkata",
    )
    assert plan.needs_memory
    assert plan.start is not None or plan.end is not None, (
        "Expected a date range for 'last week' query"
    )


async def test_no_date_range_for_general_query():
    plan = await make_plan(
        "what is my sister's name?",
        recent_chat="",
        timezone="Asia/Kolkata",
    )
    # start/end should be None — no time reference in this query
    assert plan.start is None and plan.end is None, (
        f"Unexpected date range: start={plan.start}, end={plan.end}"
    )
