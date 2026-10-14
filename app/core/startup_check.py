"""Check all external integrations at startup.

Runs after the app initialises. Failures are logged as warnings (app still starts)
and sent to the owner via Telegram so they surface in chat, not buried in terminal logs.
"""

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


async def _check_database() -> CheckResult:
    from sqlalchemy import text
    from app.core.db import session_scope
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return CheckResult("Database", True, "connected")
    except Exception as exc:
        return CheckResult("Database", False, f"cannot connect: {type(exc).__name__}")


async def _check_google() -> CheckResult:
    from app.tools.google_common import google_service
    try:
        result = await asyncio.to_thread(google_service, "calendar", "v3")
    except Exception as exc:
        return CheckResult("Google", False, f"error: {exc}")
    if result is None:
        return CheckResult("Google", False, "not connected — run `python -m scripts.google_auth`")
    if isinstance(result, str):
        return CheckResult("Google", False, result)
    return CheckResult("Google", True, "connected")


async def _check_embeddings() -> CheckResult:
    from app.core.llm import embed
    try:
        await embed("startup check")
        return CheckResult("Embeddings", True, "working")
    except Exception as exc:
        return CheckResult("Embeddings", False, f"embed failed: {type(exc).__name__}: {exc}")


async def run_all() -> list[CheckResult]:
    """Run all integration checks concurrently. Always returns results; never raises."""
    results = await asyncio.gather(
        _check_database(),
        _check_google(),
        _check_embeddings(),
    )
    return list(results)


def log_summary(results: list[CheckResult]) -> None:
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    for r in ok:
        log.info("  [ok]   %s: %s", r.name, r.detail)
    for r in bad:
        log.warning("  [FAIL] %s: %s", r.name, r.detail)


def chat_warning(results: list[CheckResult]) -> str | None:
    """Return a chat-friendly warning for the owner, or None if everything is fine."""
    bad = [r for r in results if not r.ok]
    if not bad:
        return None
    lines = ["Startup checks found issues:"]
    for r in bad:
        lines.append(f"  {r.name}: {r.detail}")
    return "\n".join(lines)
