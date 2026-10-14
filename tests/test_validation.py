"""Bad input must be rejected before it gets anywhere near the database. No database needed here."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.config import Settings
from app.memory.schemas import MemoryCreate
from app.schemas import ChatRequest, NoteCreate, TaskCreate

USER = uuid4()
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "fields",
    [
        {"content": "   "},  # blank after trimming
        {"content": "x" * 1001},  # too long
        {"content": "ok", "importance": 9},
        {"content": "ok", "kind": "gossip"},
        {"content": "ok", "event_at": datetime(2030, 1, 1)},  # no timezone
        {"content": "ok", "surprise": "field"},  # unknown field
        {"content": "ok", "replaces": ["not-a-uuid"]},
    ],
)
def test_memory_create_rejects_bad_input(fields):
    with pytest.raises(ValidationError):
        MemoryCreate(user_id=USER, **fields)


def test_memory_create_cleans_good_input():
    memory = MemoryCreate(user_id=USER, content="  Riya is my sister  ", event_at=NOW)
    assert memory.content == "Riya is my sister"
    assert memory.importance == 3 and memory.kind == "fact"


def test_task_and_note_validation():
    with pytest.raises(ValidationError):
        TaskCreate(user_id=USER, title="Pay rent", due_at=datetime(2030, 1, 1))  # naive datetime
    with pytest.raises(ValidationError):
        TaskCreate(user_id=USER, title="")
    with pytest.raises(ValidationError):
        NoteCreate(user_id=USER, title="Ideas", body="")
    TaskCreate(user_id=USER, title="Pay rent", due_at=NOW)


def test_chat_request_validation():
    with pytest.raises(ValidationError):
        ChatRequest(message="   ")
    with pytest.raises(ValidationError):
        ChatRequest(message="hi", conversation_id="nope")
    assert ChatRequest(message=" hi ").message == "hi"


def test_settings_validation():
    assert Settings(database_url="postgresql://u:p@h/db").database_url.startswith("postgresql+asyncpg://")
    with pytest.raises(ValidationError):
        Settings(database_url="mysql://u:p@h/db")
    with pytest.raises(ValidationError):
        Settings(user_timezone="Mars/Olympus")


def test_api_rejects_bad_requests_before_doing_anything():
    from app.main import app

    client = TestClient(app)  # no `with`: the app never connects to a database
    assert client.post("/chat", json={"message": "  "}).status_code == 422
    assert client.post("/chat", json={"message": "hi", "conversation_id": "x"}).status_code == 422
    assert client.post("/chat", json={"message": "hi", "extra": 1}).status_code == 422
    assert client.get("/conversations/not-a-uuid/messages").status_code == 422
    assert client.delete("/memories/not-a-uuid").status_code == 422
    assert client.get("/tasks", params={"status": "maybe"}).status_code == 422
    assert client.get("/memories", params={"q": ""}).status_code == 422


async def test_tool_calls_with_bad_arguments_are_sent_back_to_the_model():
    """The model gets an error message and can retry; nothing is written."""
    from langgraph.prebuilt import create_react_agent

    from app.tools import remember
    from tests.fakes import ScriptedChatModel

    bad_call = {"name": "remember", "args": {"content": "ok", "importance": 9}, "id": "c1"}
    model = ScriptedChatModel(
        script=[AIMessage(content="", tool_calls=[bad_call]), AIMessage(content="Sorry, retrying.")]
    )
    agent = create_react_agent(model, [remember])
    result = await agent.ainvoke(
        {"messages": [("user", "remember something")]}, config={"configurable": {"user": None}}
    )
    tool_reply = next(m for m in result["messages"] if m.type == "tool")
    assert tool_reply.status == "error"
    assert "importance" in tool_reply.content
    assert result["messages"][-1].content == "Sorry, retrying."  # the loop carried on
