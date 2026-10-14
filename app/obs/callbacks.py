"""A LangChain callback that records every model call and tool call as a span.

It is attached to each chat model (see llm.chat_model), so model calls are seen wherever they happen:
inside the agent graph, in the memory router, in extraction, in the reranker. Tool calls are seen
because the same handler is also passed in the graph's run config.
"""

import logging
import time
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

from app.obs import trace as obs
from app.core.privacy import scrub as scrub_pii

log = logging.getLogger(__name__)


def _agent_from(metadata: dict | None) -> str:
    """Which agent in the team made this call, e.g. 'planner', from LangGraph's namespace."""
    ns = (metadata or {}).get("langgraph_checkpoint_ns", "")
    names = [part.split(":")[0] for part in ns.split("|") if part]
    if len(names) >= 3:
        return names[-2]
    return names[-1] if names else ""


def _purpose(tags: list[str] | None) -> str:
    return next((t.split(":", 1)[1] for t in tags or [] if t.startswith("purpose:")), "")


class ObsCallbackHandler(AsyncCallbackHandler):
    raise_error = False  # telemetry problems must never fail a model call
    run_inline = True  # run in the caller's task, so the current trace is visible

    def __init__(self):
        self.open: dict[UUID, obs.Span] = {}

    # ---- model calls
    async def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        if run_id in self.open:  # the same handler can be reached twice (model level and run config)
            return
        try:
            agent = _agent_from(metadata)
            purpose = _purpose(tags)
            model = (metadata or {}).get("ls_model_name") or (kwargs.get("invocation_params") or {}).get("model_id")
            batch = messages[0] if messages else []
            last = batch[-1] if batch else None
            s = obs.Span(kind="llm", name=agent or purpose or "llm", model=model)
            s.attrs.update(
                purpose=purpose, agent=agent, messages=len(batch),
                prompt_chars=sum(len(str(getattr(m, "content", ""))) for m in batch),
                last_input=scrub_pii(obs.clip(getattr(last, "content", "") if last is not None else "", 500)),
            )
            s.attrs["_first_token"] = None
            self.open[run_id] = s
        except Exception:
            log.debug("obs callback failed", exc_info=True)

    async def on_llm_new_token(self, token, *, run_id, **kwargs):
        s = self.open.get(run_id)
        if s is not None and s.attrs.get("_first_token") is None:
            s.attrs["_first_token"] = round((time.perf_counter() - s.t0) * 1000)

    async def on_llm_end(self, response, *, run_id, **kwargs):
        s = self.open.pop(run_id, None)
        if s is None:
            return
        try:
            message = response.generations[0][0].message
            usage = getattr(message, "usage_metadata", None) or {}
            if not usage and response.llm_output:
                usage = response.llm_output.get("usage") or response.llm_output.get("token_usage") or {}
            s.input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            s.output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            tool_calls = [c.get("name") for c in (getattr(message, "tool_calls", None) or [])]
            s.attrs["requested_tools"] = tool_calls
            s.attrs["output"] = scrub_pii(obs.clip(getattr(message, "content", "") if not tool_calls else f"-> {tool_calls}", 500))
            s.attrs["stop_reason"] = (getattr(message, "response_metadata", {}) or {}).get("stopReason")
        except Exception:
            log.debug("obs callback failed", exc_info=True)
        self._close(s)

    async def on_llm_error(self, error, *, run_id, **kwargs):
        s = self.open.pop(run_id, None)
        if s is not None:
            self._close(s, error)

    # ---- tool calls
    async def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, metadata=None, inputs=None, **kwargs):
        if run_id in self.open:
            return
        try:
            name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
            s = obs.Span(kind="tool", name=name)
            s.attrs.update(args=scrub_pii(obs.clip(inputs if inputs is not None else input_str, 800)), agent=_agent_from(metadata))
            self.open[run_id] = s
        except Exception:
            log.debug("obs callback failed", exc_info=True)

    async def on_tool_end(self, output, *, run_id, **kwargs):
        s = self.open.pop(run_id, None)
        if s is None:
            return
        text = str(getattr(output, "content", output))
        s.attrs["result"] = scrub_pii(obs.clip(text, 800))
        failed = getattr(output, "status", "") == "error" or text.startswith(("Error", "Tool error"))
        self._close(s, text[:300] if failed else None)

    async def on_tool_error(self, error, *, run_id, **kwargs):
        s = self.open.pop(run_id, None)
        if s is not None:
            self._close(s, error)

    def _close(self, s: obs.Span, error=None) -> None:
        first = s.attrs.pop("_first_token", None)
        if first is not None:
            s.attrs["ttft_ms"] = first
        s.finish(error)
        obs.record(s)


handler = ObsCallbackHandler()
