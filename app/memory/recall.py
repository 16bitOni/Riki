"""The memory router: decide what to look up, look it up, and format it for the LLM."""

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from app.core.llm import chat_model
from app.memory.retrieval import hybrid_search
from app.memory.schemas import RetrievalPlan
from app.obs import trace as obs
from app.core.timeutil import localize, now_in

PLANNER_PROMPT = """You decide how to search a person's long-term memory so an assistant can answer their latest message.
Current date and time: {now} (timezone {timezone}).

First: resolve references. If the latest message says "these companies", "that role", "the thing
I mentioned" — look at the recent chat to figure out what they mean. Use the resolved names and
topics (not the pronouns) when filling in the fields below.

- needs_memory: false for greetings, thanks, or messages that need nothing personal.
- keywords: exact words likely stored in a memory (names, places, objects). Use a few strong ones.
- semantic_query: a rewrite capturing the meaning, for things like preferences, opinions or plans.
- entities: people, places, organisations or projects mentioned or referenced.
- relation_words: relationship words ("my sister's husband's company" -> sister, husband, company). Use the user's own words.
- start / end: only if the message refers to a time ("last week", "in March", "yesterday"). Use ISO 8601 with the timezone offset.
Leave fields empty rather than guessing."""


async def make_plan(message: str, recent_chat: str, timezone: str) -> RetrievalPlan:
    llm = chat_model(fast=True, purpose="recall_plan").with_structured_output(RetrievalPlan)
    plan = await llm.ainvoke(
        [
            SystemMessage(PLANNER_PROMPT.format(now=now_in(timezone).isoformat(), timezone=timezone)),
            HumanMessage(f"Recent chat:\n{recent_chat or '(none)'}\n\nLatest message:\n{message}"),
        ]
    )
    plan.start = localize(plan.start, timezone)
    plan.end = localize(plan.end, timezone)
    return plan


def format_memories(memories) -> str:
    if not memories:
        return "(nothing found)"
    lines = []
    for m in memories:
        when = (m.event_at or m.created_at).strftime("%Y-%m-%d")
        lines.append(f"- [{m.kind}, {when}] {m.content}  (id: {m.id})")
    return "\n".join(lines)


async def search(user, query: str, recent_chat: str = ""):
    """Plan + hybrid search. Returns the Memory rows found."""
    with obs.span("recall", "recall", query=obs.clip(query, 300)) as span:
        plan = await make_plan(query, recent_chat, user.timezone)
        span.attrs["needs_memory"] = plan.needs_memory
        if not plan.needs_memory:
            return []
        found = await hybrid_search(user.id, plan, settings.recall_limit, rerank_query=query)
        span.attrs["returned"] = len(found)
        return found


async def build_memory_context(user, message: str, recent_chat: str) -> tuple[str, list]:
    """Text injected into the system prompt on every request, plus the memories it contains."""
    memories = await search(user, message, recent_chat)
    context = (
        f"## About {user.name}\n{user.profile or '(no profile yet)'}\n\n"
        f"## Memories that may be relevant to this message\n{format_memories(memories)}"
    )
    return context, memories
