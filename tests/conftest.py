import pytest
from alembic import command
from alembic.config import Config

from tests.fakes import fake_embed


@pytest.fixture(scope="session")
def migrated_database():
    """Bring the database to the latest schema once per test run (skip if Postgres is not running)."""
    try:
        command.upgrade(Config("alembic.ini"), "head")
    except Exception as error:
        pytest.skip(f"database not reachable or migration failed: {error}")


@pytest.fixture
async def user(migrated_database, monkeypatch):
    """A throwaway user in the real database. Deleting it removes everything created for it."""
    from sqlalchemy import delete

    from app.db import engine, session_scope
    from app.memory import retrieval, store
    from app.models import User

    monkeypatch.setattr(store, "embed", fake_embed)  # no AWS calls in tests
    monkeypatch.setattr(retrieval, "embed", fake_embed)

    async with session_scope() as session:
        created = User(name="Test User")
        session.add(created)
        await session.flush()
    yield created
    async with session_scope() as session:
        await session.execute(delete(User).where(User.id == created.id))
    await engine.dispose()  # each test has its own event loop; do not reuse pooled connections


@pytest.fixture(autouse=True)
async def dispose_engine_after_each_test():
    """Each test has its own event loop, so a pooled connection must never outlive its test.
    Anything that touches the database (telemetry writes included) is covered, not just the `user` fixture."""
    yield
    from app.db import engine

    await engine.dispose()
