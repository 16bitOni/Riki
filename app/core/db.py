"""Database connection and sessions."""

from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.models import User

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=8,
    max_overflow=4,
    pool_timeout=30,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope():
    """One transaction: committed if the block finishes, rolled back if it raises."""
    async with SessionLocal() as session:
        async with session.begin():
            yield session


async def get_or_create_user() -> User:
    """Single-user mode: the assistant belongs to the person named in .env."""
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.name == settings.user_name))
        if user is None:
            user = User(name=settings.user_name, timezone=settings.user_timezone)
            session.add(user)
            await session.flush()
        return user


async def assert_embedding_dimension() -> None:
    """Fail at startup if EMBEDDING_DIM differs from the database column (the vector size is fixed in the schema)."""
    async with session_scope() as session:
        column_dim = await session.scalar(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'memories'::regclass AND attname = 'embedding'"
            )
        )
    if column_dim is not None and column_dim != settings.embedding_dim:
        raise RuntimeError(
            f"EMBEDDING_DIM is {settings.embedding_dim} but the database column holds {column_dim} dimensions. "
            "Changing the dimension needs an Alembic migration and a full re-embed (see docs/ARCHITECTURE.md)."
        )


__all__ = ["engine", "session_scope", "get_or_create_user", "assert_embedding_dimension", "AsyncSession"]
