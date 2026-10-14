"""Shared helpers for eval tests.

Eval tests use the real LLM — they test model decision quality, not code wiring.
They are slow (~10-30s each) and cost tokens.

Run the full eval suite:
    uv run pytest tests/eval/ -v

Run a single file:
    uv run pytest tests/eval/test_routing.py -v
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

# A fake user that needs no database.
EVAL_USER = SimpleNamespace(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    name="Subhadip",
    timezone="Asia/Kolkata",
    profile="Subhadip is a software engineer in India who likes to travel and has a close friend named Subho (Subhodip Ghosal).",
)

EVAL_CONFIG = {
    "configurable": {
        "user": EVAL_USER,
        "turn_key": "eval/test",
        "images": [],
    },
    "recursion_limit": 15,
}


@pytest.fixture
def eval_user():
    return EVAL_USER


@pytest.fixture
def eval_config():
    return EVAL_CONFIG


@pytest.fixture(autouse=True)
def stub_db_tools(monkeypatch):
    """Stub all tool functions that write to or read from Postgres.

    Routing tests only care about WHICH specialist is called, not what the tool does.
    These stubs prevent FK violations from the fake user UUID.
    """
    import app.planner as planner_module
    import app.reminders as rem_module
    import app.memory.store as store_module
    import app.memory.recall as recall_module
    from tests.fakes import fake_embed

    # reminders
    async def _fake_create_reminder(user_id, text, due):
        return SimpleNamespace(id="00000000-0000-0000-0000-aaaaaaaaaaaa")

    monkeypatch.setattr(rem_module, "create_reminder", _fake_create_reminder)

    # planner (tasks + notes) — patch the app.planner module functions
    async def _fake_create_task(data):
        return SimpleNamespace(id="00000000-0000-0000-0000-bbbbbbbbbbbb")

    async def _fake_list_tasks(user_id, status="open"):
        return []

    async def _fake_complete_task(user_id, task_id):
        return True

    async def _fake_create_note(data):
        return SimpleNamespace(id="00000000-0000-0000-0000-cccccccccccc")

    async def _fake_search_notes(user_id, query):
        return []

    for name, fn in [
        ("create_task", _fake_create_task),
        ("list_tasks", _fake_list_tasks),
        ("complete_task", _fake_complete_task),
        ("create_note", _fake_create_note),
        ("search_notes", _fake_search_notes),
    ]:
        if hasattr(planner_module, name):
            monkeypatch.setattr(planner_module, name, fn)

    # memory (remember / search / forget)
    async def _fake_save_memory(data):
        return SimpleNamespace(id="00000000-0000-0000-0000-dddddddddddd")

    async def _fake_search(user, query, recent_chat=""):
        return []

    async def _fake_forget(user_id, memory_id):
        return True

    monkeypatch.setattr(store_module, "save_memory", _fake_save_memory)
    monkeypatch.setattr(store_module, "embed", fake_embed)
    monkeypatch.setattr(recall_module, "search", _fake_search)
    monkeypatch.setattr(store_module, "forget_memory", _fake_forget)
