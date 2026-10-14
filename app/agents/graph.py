"""The agent graph, built with LangGraph.

    START -> trim -> recall -> assistant (single ReAct agent with all tools) -> END

`trim` caps the message list so long sessions don't blow the context window.
`recall` runs the memory router once per message and injects profile, memories and time.

With checkpointing enabled, the full graph state persists across turns within a
conversation. Follow-ups work naturally because the LLM sees the complete history.
"""

from langchain_core.messages import RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.prebuilt.chat_agent_executor import AgentState

from app.agents import prompts
from app.core.checkpoint import get_checkpointer
from app.config import settings
from app.core.llm import chat_model, text_of
from app.memory.recall import build_memory_context
from app.cognition import get_reflection_context
from app.chat.session import get_session_guide_context
from app.core.timeutil import now_in
from app.tools import ALL_TOOLS

# Claude Sonnet context is ~200k tokens. Reserve ~40k for system prompt + memories + response.
# Rough token estimate: 1 token ≈ 4 chars for English text.
_MAX_HISTORY_CHARS = 120_000   # ~30k tokens of conversation history
_TOOL_OUTPUT_CAP = 8_000       # truncate any single tool output beyond this


class ChatState(MessagesState):
    """State of the outer graph. `messages` comes from MessagesState."""

    memory_context: str
    recalled: list  # IDs (str) of memories shown to the agent this turn


class AgentInnerState(AgentState):
    """Passed into the ReAct agent (AgentState adds the loop counter it needs)."""

    memory_context: str
    recalled: list


def _prompt(state: AgentInnerState):
    """System prompt = Riki instructions + this turn's memory context, then the chat."""
    system = f"{prompts.RIKI}\n\n{state['memory_context']}"
    return [SystemMessage(system), *state["messages"]]


def _msg_chars(msg) -> int:
    """Rough character count of a message."""
    content = msg.content
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "") if isinstance(b, dict) else str(b)) for b in content)
    return 0


def _truncate_tool_outputs(messages: list) -> list:
    """Cap oversized tool outputs so one big response can't blow the context."""
    result = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and _msg_chars(msg) > _TOOL_OUTPUT_CAP:
            truncated = msg.content[:_TOOL_OUTPUT_CAP] if isinstance(msg.content, str) else str(msg.content)[:_TOOL_OUTPUT_CAP]
            msg = ToolMessage(
                content=truncated + f"\n[truncated — original was {_msg_chars(msg)} chars]",
                tool_call_id=msg.tool_call_id,
                id=msg.id,
            )
        result.append(msg)
    return result


async def trim_node(state: ChatState, config) -> dict:
    """Drop oldest messages when history is too large (token-aware, not just count-aware).

    Also enforces a hard message count cap as a secondary safety net.
    """
    messages = state["messages"]
    if not messages:
        return {}

    # First: cap oversized tool outputs in-place
    capped = _truncate_tool_outputs(messages)
    removals = []

    # Compute total chars from oldest → newest, keep from the end
    # Always keep at least the last 6 messages (current turn + recent context)
    keep_min = 6
    total_chars = sum(_msg_chars(m) for m in capped)

    if total_chars > _MAX_HISTORY_CHARS:
        # Walk from oldest, mark for removal until we're under budget
        running = total_chars
        cutoff_idx = 0
        for i, msg in enumerate(capped):
            if i >= len(capped) - keep_min:
                break
            running -= _msg_chars(msg)
            cutoff_idx = i + 1
            if running <= _MAX_HISTORY_CHARS:
                break
        to_remove = capped[:cutoff_idx]
        removals = [RemoveMessage(id=m.id) for m in to_remove if hasattr(m, "id") and m.id]

    # Secondary: hard message count cap
    count_limit = settings.session_max_messages
    remaining = [m for m in capped if not any(
        r.id == getattr(m, "id", None) for r in removals
    )]
    if len(remaining) > count_limit:
        extra = remaining[: len(remaining) - count_limit]
        extra_removals = [RemoveMessage(id=m.id) for m in extra if hasattr(m, "id") and m.id]
        removals.extend(extra_removals)

    return {"messages": removals} if removals else {}


async def recall_node(state: ChatState, config) -> dict:
    user = config["configurable"]["user"]
    *earlier, latest = state["messages"]
    recent_chat = "\n".join(f"{m.type}: {text_of(m)}" for m in earlier[-8:])
    context, memories = await build_memory_context(user, text_of(latest), recent_chat)

    now = now_in(user.timezone)
    header = f"Current date and time: {now.strftime('%A %Y-%m-%d %H:%M %Z')}\n"

    last_at = config["configurable"].get("last_message_at")
    if last_at:
        from datetime import datetime, timezone
        gap_hours = (datetime.now(timezone.utc) - last_at).total_seconds() / 3600
        if gap_hours >= 1:
            header += f"Last message in this conversation was {int(gap_hours)} hours ago.\n"
    header += "\n"

    guide_context = await get_session_guide_context(user)
    reflection_context = await get_reflection_context(user)
    recalled_ids = [str(m.id) for m in memories]
    return {"memory_context": header + context + guide_context + reflection_context, "recalled": recalled_ids}


def build_graph(model=None, checkpointer=None):
    """`model` can be replaced in tests; by default the main Bedrock model is used.
    `checkpointer` enables persistent sessions when provided.
    """
    model = model or chat_model(purpose="agent")

    agent = create_react_agent(
        model,
        ALL_TOOLS,
        name="riki",
        prompt=_prompt,
        state_schema=AgentInnerState,
    )

    graph = StateGraph(ChatState)
    graph.add_node("trim", trim_node)
    graph.add_node("recall", recall_node)
    graph.add_node("assistant", agent)
    graph.add_edge(START, "trim")
    graph.add_edge("trim", "recall")
    graph.add_edge("recall", "assistant")
    graph.add_edge("assistant", END)
    return graph.compile(checkpointer=checkpointer)


_graph = None


def init_graph(model=None):
    """Called once at startup after the checkpointer is ready."""
    global _graph
    _graph = build_graph(model=model, checkpointer=get_checkpointer())
    return _graph


def get_graph():
    if _graph is None:
        return init_graph()
    return _graph
