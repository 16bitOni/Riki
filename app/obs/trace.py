"""Traces and spans.

A Trace is one unit of work (a chat turn, a memory-extraction run, an operator run). Code anywhere
below it records Spans (a model call, a tool call, a retrieval) through `span()`; the current trace is
found through a context variable, so nothing has to be passed around. At the end the trace and all
its spans are written to Postgres in one transaction.
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import settings
from app.obs.pricing import cost_usd

log = logging.getLogger(__name__)

_current: ContextVar["Trace | None"] = ContextVar("obs_trace", default=None)
ACTIVE: dict[uuid.UUID, "Trace"] = {}  # traces that have started and not yet finished (the "in flight" view)
_tasks: set[asyncio.Task] = set()  # keeps fire-and-forget writes from being garbage collected


def clip(text, limit: int | None = None) -> str | None:
    """Shorten text for storage; None passes through. With telemetry_store_content off, only sizes are kept."""
    if text is None:
        return None
    text = str(text)
    if not settings.telemetry_store_content:
        return f"[{len(text)} chars, content storage is off]"
    limit = limit or 2000
    return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} chars]"


def jsonable(value):
    """Round-trip through JSON so whatever lands in a JSONB column is guaranteed storable."""
    return json.loads(json.dumps(value, default=str))


@dataclass
class Span:
    kind: str
    name: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    t0: float = field(default_factory=time.perf_counter)
    duration_ms: int = 0
    status: str = "ok"
    error: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    attrs: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def finish(self, error: BaseException | str | None = None) -> "Span":
        self.duration_ms = round((time.perf_counter() - self.t0) * 1000)
        if error is not None:
            is_exception = isinstance(error, BaseException)
            self.status = "error"
            self.error = clip(f"{type(error).__name__}: {error}" if is_exception else error, 1000)
            self.attrs.setdefault("error_type", type(error).__name__ if is_exception else "error")
        return self

    @property
    def cost(self) -> float:
        return cost_usd(self.model, self.input_tokens, self.output_tokens)


@dataclass
class Trace:
    kind: str
    source: str = ""
    conversation_id: uuid.UUID | None = None
    user_text: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    t0: float = field(default_factory=time.perf_counter)
    spans: list[Span] = field(default_factory=list)
    reply_text: str | None = None
    status: str = "ok"
    error: str | None = None
    recalled: int = 0
    meta: dict = field(default_factory=dict)

    def fail(self, error: BaseException | str) -> None:
        self.status = "error"
        self.error = clip(f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error, 1000)


def current() -> Trace | None:
    return _current.get()


def start(kind: str, source: str = "", user_text: str | None = None, conversation_id=None) -> Trace | None:
    """Begin a trace and make it the current one for this task (and tasks it spawns)."""
    if not settings.telemetry_enabled:
        return None
    trace = Trace(kind=kind, source=source, user_text=clip(user_text), conversation_id=conversation_id)
    ACTIVE[trace.id] = trace
    _current.set(trace)
    return trace


async def finish(trace: Trace | None) -> None:
    """Close the trace, write it with its spans, and stop it being the current one. Never raises."""
    if trace is None:
        return
    ACTIVE.pop(trace.id, None)
    if _current.get() is trace:
        _current.set(None)
    try:
        await _write(trace)
    except Exception:
        log.warning("could not write a trace to the database", exc_info=True)


async def _write(trace: Trace) -> None:
    from app.core.db import session_scope
    from app.core.models import ObsTrace

    llm = [s for s in trace.spans if s.kind == "llm"]
    row = ObsTrace(
        id=trace.id, kind=trace.kind, source=trace.source, conversation_id=trace.conversation_id,
        started_at=trace.started_at, duration_ms=round((time.perf_counter() - trace.t0) * 1000),
        status=trace.status, error=trace.error, user_text=trace.user_text, reply_text=clip(trace.reply_text),
        input_tokens=sum(s.input_tokens for s in llm), output_tokens=sum(s.output_tokens for s in llm),
        llm_calls=len(llm), tool_calls=sum(1 for s in trace.spans if s.kind == "tool"),
        cost_usd=sum(s.cost for s in trace.spans), recalled=trace.recalled, meta=jsonable(trace.meta),
    )
    async with session_scope() as session:
        session.add(row)
        await session.flush()  # the trace row must exist before the spans that point at it
        session.add_all(_span_row(s, trace.id) for s in trace.spans)


def _span_row(s: Span, trace_id):
    from app.core.models import ObsSpan

    return ObsSpan(
        id=s.id, trace_id=trace_id, kind=s.kind, name=s.name[:120], started_at=s.started_at,
        duration_ms=s.duration_ms, status=s.status, error=s.error, model=s.model,
        input_tokens=s.input_tokens, output_tokens=s.output_tokens, cost_usd=s.cost, attrs=jsonable(s.attrs),
    )


def record(s: Span) -> None:
    """Attach a finished span to the current trace, or store it on its own if there is none."""
    trace = _current.get()
    if trace is not None:
        trace.spans.append(s)
        return
    if not settings.telemetry_enabled:
        return
    try:
        task = asyncio.get_running_loop().create_task(_write_loose(s))
    except RuntimeError:  # no event loop (a sync caller): drop it
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _write_loose(s: Span) -> None:
    from app.core.db import session_scope

    try:
        async with session_scope() as session:
            session.add(_span_row(s, None))
    except Exception:
        log.warning("could not write a span to the database", exc_info=True)


@contextmanager
def span(kind: str, name: str, **attrs):
    """Time a block. Yields the Span so the block can add attrs, tokens or a model name.
    An exception marks the span as failed and is re-raised."""
    s = Span(kind=kind, name=name, attrs=dict(attrs))
    try:
        yield s
    except BaseException as error:
        s.finish(error)
        record(s)
        raise
    else:
        s.finish()
        record(s)
