"""Checks the LangGraph wiring (recall -> single ReAct agent with tools) with a scripted model."""

from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.agents import graph as graph_module
from tests.fakes import ScriptedChatModel


def tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


async def test_agent_calls_tool_and_answers(monkeypatch):
    async def fake_context(user, message, recent_chat):
        return "## About Test\n(profile)", []

    monkeypatch.setattr(graph_module, "build_memory_context", fake_context)

    model = ScriptedChatModel(
        script=[
            tool_call("get_current_time", {}, "t1"),                # agent uses a tool
            AIMessage(content="Nothing is planned for today."),     # agent answers the user
        ]
    )
    team = graph_module.build_graph(model)
    user = SimpleNamespace(id="u1", name="Test", timezone="Asia/Kolkata", profile="")

    result = await team.ainvoke(
        {"messages": [("user", "What's on today?")]},
        config={"configurable": {"user": user, "turn_key": "k"}},
    )

    assert result["messages"][-1].content == "Nothing is planned for today."
    assert model.calls == 2
    assert result["memory_context"].startswith("Current date and time:")


async def test_tools_receive_the_user_through_config():
    """Tools read the user from the run config; the model never sees that argument."""
    from app.tools import get_current_time, remember

    user = SimpleNamespace(id="u1", name="Test", timezone="Asia/Kolkata", profile="")
    text = await get_current_time.ainvoke({}, config={"configurable": {"user": user}})
    assert "UTC+0530" in text
    assert "config" not in remember.args  # hidden from the LLM's tool schema
