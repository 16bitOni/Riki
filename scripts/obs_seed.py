"""Fill the dashboard with realistic synthetic telemetry, so you can see it without real traffic.

    python -m scripts.obs_seed                 # 72 hours of fake traffic
    python -m scripts.obs_seed --hours 168     # a week
    python -m scripts.obs_seed --clear         # remove everything this script created

Every row it writes is marked meta.synthetic = true (traces) or attrs.synthetic = true (spans), so
--clear removes exactly those and never touches real data. Real memories are referenced when present.
"""

import argparse
import asyncio
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text

from app.db import engine, session_scope
from app.models import Memory, ObsSpan, ObsTrace
from app.obs.pricing import cost_usd

MAIN, FAST, EMBED = "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-haiku-4-5-20251001-v1:0", "amazon.titan-embed-text-v2:0"

QUESTIONS = [
    ("what's on my plate tomorrow?", ["planner"], ["list_tasks", "list_calendar_events"]),
    ("remind me to call mom at 6pm", ["planner"], ["add_task"]),
    ("who is riya again?", [], []),
    ("what did I decide about the pune trip", ["memory_keeper"], ["search_memory"]),
    ("latest news on the bedrock pricing", ["researcher"], ["web_search"]),
    ("hey", [], []),
    ("remember that my sister's birthday is on march 4", ["memory_keeper"], ["remember"]),
    ("add a note: ideas for the dashboard", ["planner"], ["add_note"]),
    ("open youtube in chrome and play some lofi", ["operator"], ["use_computer"]),
    ("summarise what I told you about my job search", ["memory_keeper"], ["search_memory"]),
    ("what's my schedule next week", ["planner"], ["get_current_time", "list_calendar_events"]),
    ("thanks!", [], []),
    ("how do I center a div", [], []),
    ("where does my sister's husband work", [], []),
]
PLANS = [
    {"keywords": ["Riya"], "semantic_query": None, "entities": ["Riya"], "relation_words": []},
    {"keywords": ["Pune", "trip"], "semantic_query": "decision about the Pune trip", "entities": [], "relation_words": []},
    {"keywords": [], "semantic_query": "preferences about working hours", "entities": [], "relation_words": []},
    {"keywords": ["birthday"], "semantic_query": None, "entities": [], "relation_words": ["sister"]},
    {"keywords": [], "semantic_query": None, "entities": [], "relation_words": ["sister", "husband", "company"]},
]
KINDS = ["fact", "fact", "preference", "event", "episode"]


def gauss_ms(mean: float, spread: float = 0.35) -> int:
    return max(40, int(random.lognormvariate(math.log(mean), spread)))


def traffic_weight(moment: datetime) -> float:
    """More chats in the evening (IST-ish), few at night."""
    hour = (moment.hour + 5) % 24
    return 0.15 + 1.4 * math.exp(-((hour - 21) ** 2) / 18) + 0.6 * math.exp(-((hour - 11) ** 2) / 10)


class Builder:
    def __init__(self, memory_ids: list[tuple[uuid.UUID, str, str, int]]):
        self.memories = memory_ids
        self.traces: list[ObsTrace] = []
        self.spans: list[ObsSpan] = []

    def span(self, trace_id, kind, name, at, ms, status="ok", error=None, model=None, tin=0, tout=0, **attrs):
        attrs["synthetic"] = True
        self.spans.append(ObsSpan(
            id=uuid.uuid4(), trace_id=trace_id, kind=kind, name=name, started_at=at, duration_ms=ms, status=status,
            error=error, model=model, input_tokens=tin, output_tokens=tout, cost_usd=cost_usd(model, tin, tout), attrs=attrs,
        ))
        return self.spans[-1]

    def retrieval(self, trace_id, at, plan):
        channels, results, total = {}, [], 0
        if plan["keywords"]:
            channels["keyword"] = {"hits": random.choice([0, 1, 2, 4, 6, 9]), "ms": gauss_ms(14), "error": None, "weight": 1.0}
        if plan["semantic_query"]:
            channels["semantic"] = {"hits": random.choice([0, 3, 5, 8, 12, 17]), "ms": gauss_ms(180), "error": None, "weight": 1.0}
        if plan["entities"] or plan["relation_words"]:
            channels["graph"] = {"hits": random.choice([0, 2, 5, 11]), "ms": gauss_ms(26), "error": None, "weight": 0.7}
        if random.random() < 0.12 and "semantic" in channels:
            channels["semantic"].update(hits=0, error="ThrottlingException")
        for c in channels.values():
            total += c["hits"]
        returned = min(8, max(0, total - random.randint(0, 3)))
        pool = random.sample(self.memories, min(returned, len(self.memories))) if self.memories else []
        returned = len(pool) if self.memories else returned
        for rank, memory in enumerate(pool, start=1):
            found = {name: random.randint(1, 12) for name in channels if random.random() < 0.6} or {next(iter(channels), "keyword"): rank}
            rrf = round(sum(1 / (60 + p) for p in found.values()), 5)
            results.append({
                "id": str(memory[0]), "rank": rank, "kind": memory[2], "importance": memory[3], "rrf": rrf,
                "score": round(rrf * (1 + 0.05 * (memory[3] - 3)), 5), "found_by": found, "moved_by_rerank": 0, "text": memory[1][:160],
            })
        results.sort(key=lambda r: -r["score"])
        for i, r in enumerate(results, start=1):
            r["rank"] = i
        ms = max((c["ms"] for c in channels.values()), default=5) + gauss_ms(20)
        self.span(trace_id, "retrieval", "hybrid_search", at, ms, limit=8, channels=channels, plan=plan, candidates=total,
                  reranked=False, returned=len(results), results=results)
        return len(results), ms

    def chat(self, at: datetime, source: str):
        question, agents, tools = random.choice(QUESTIONS)
        trace_id = uuid.uuid4()
        cursor, spans_before = at, len(self.spans)
        fail = random.random() < 0.045
        error = None

        # recall: plan + search (+ embedding for semantic)
        needs = bool(agents) or random.random() < 0.5
        plan_ms = gauss_ms(620)
        plan_tin, plan_tout = random.randint(380, 520), random.randint(40, 110)
        self.span(trace_id, "llm", "recall_plan", cursor, plan_ms, model=FAST, tin=plan_tin, tout=plan_tout, purpose="recall_plan",
                  agent="", last_input=question, output="structured plan")
        cursor += timedelta(milliseconds=plan_ms)
        recalled = 0
        plan = random.choice(PLANS)
        if needs:
            if plan["semantic_query"]:
                self.span(trace_id, "embed", "embed", cursor, gauss_ms(130), model=EMBED, tin=random.randint(6, 24), chars=60)
            recalled, ms = self.retrieval(trace_id, cursor, plan)
            cursor += timedelta(milliseconds=ms)
        self.span(trace_id, "recall", "recall", at, int((cursor - at).total_seconds() * 1000), query=question, needs_memory=needs,
                  returned=recalled)

        # supervisor + specialists
        in_tokens = random.randint(1400, 2600) + recalled * 60
        for agent in ["supervisor"] + agents:
            ms = gauss_ms(2100 if agent == "supervisor" else 1500)
            throttled = fail and random.random() < 0.5 and agent == "supervisor"
            if throttled:
                self.span(trace_id, "llm", agent, cursor, 420, status="error", error="ThrottlingException: Too many requests", model=MAIN,
                          purpose="agent", agent=agent, error_type="ThrottlingException", last_input=question)
                error = "ThrottlingException: Too many requests"
                cursor += timedelta(milliseconds=420)
                break
            tin, tout = in_tokens + random.randint(-200, 300), random.randint(60, 420)
            first = int(ms * random.uniform(0.18, 0.4))
            self.span(trace_id, "llm", agent, cursor, ms, model=MAIN, tin=tin, tout=tout, purpose="agent", agent=agent,
                      ttft_ms=first, last_input=question, output="...", requested_tools=tools if agent != "supervisor" else agents[:1])
            cursor += timedelta(milliseconds=ms)
            if agent != "supervisor":
                for tool in tools:
                    tool_ms = {"web_search": gauss_ms(1700), "use_computer": gauss_ms(21000)}.get(tool, gauss_ms(90))
                    tool_failed = random.random() < (0.22 if tool == "list_calendar_events" else 0.04)
                    self.span(trace_id, "tool", tool, cursor, tool_ms, status="error" if tool_failed else "ok",
                              error="HttpError 401: invalid_grant (token expired)" if tool_failed else None, agent=agent,
                              args=f'{{"query": "{question[:40]}"}}', result="Token has been expired or revoked." if tool_failed else "ok",
                              error_type="HttpError" if tool_failed else None)
                    cursor += timedelta(milliseconds=tool_ms)
                ms2 = gauss_ms(900)
                self.span(trace_id, "llm", "supervisor", cursor, ms2, model=MAIN, tin=in_tokens + 900, tout=random.randint(40, 180),
                          purpose="agent", agent="supervisor", ttft_ms=int(ms2 * 0.3), last_input="(agent report)", output="...")
                cursor += timedelta(milliseconds=ms2)

        duration = int((cursor - at).total_seconds() * 1000)
        mine = self.spans[spans_before:]
        llm = [s for s in mine if s.kind == "llm"]
        self.traces.append(ObsTrace(
            id=trace_id, kind="chat", source=source, started_at=at, duration_ms=duration, status="error" if error else "ok", error=error,
            user_text=question, reply_text=None if error else "(synthetic reply)", input_tokens=sum(s.input_tokens for s in llm),
            output_tokens=sum(s.output_tokens for s in llm), llm_calls=len(llm), tool_calls=sum(1 for s in mine if s.kind == "tool"),
            cost_usd=sum(s.cost_usd for s in mine), recalled=recalled, meta={"synthetic": True},
        ))
        if not error and random.random() < 0.8:
            self.extraction(cursor + timedelta(milliseconds=200), question)

    def extraction(self, at, question):
        trace_id = uuid.uuid4()
        proposed = random.choice([0, 0, 0, 1, 1, 2])
        failed = random.random() < 0.03
        ms = gauss_ms(900)
        s_before = len(self.spans)
        self.span(trace_id, "llm", "extraction", at, ms, status="error" if failed else "ok", model=FAST, tin=random.randint(700, 1500),
                  tout=random.randint(20, 220), purpose="extraction", error="ValidationException: malformed tool output" if failed else None,
                  error_type="ValidationException" if failed else None)
        cursor = at + timedelta(milliseconds=ms)
        saved = 0 if failed else proposed
        for _ in range(saved):
            self.span(trace_id, "db", "save_memory", cursor, gauss_ms(60), memory_kind=random.choice(KINDS), importance=random.randint(2, 5), text="synthetic memory")
            cursor += timedelta(milliseconds=60)
        mine = self.spans[s_before:]
        self.traces.append(ObsTrace(
            id=trace_id, kind="extraction", source="background", started_at=at, duration_ms=int((cursor - at).total_seconds() * 1000),
            status="error" if failed else "ok", error="ValidationException: malformed tool output" if failed else None, user_text=question,
            reply_text="; ".join(["Subhadip likes quiet cafes"] * saved) or "(nothing worth remembering)",
            input_tokens=sum(s.input_tokens for s in mine), output_tokens=sum(s.output_tokens for s in mine), llm_calls=1,
            cost_usd=sum(s.cost_usd for s in mine), meta={"synthetic": True, "proposed": proposed, "saved": saved},
        ))

    def operator_run(self, at):
        trace_id = uuid.uuid4()
        goal = random.choice(["Open a new Chrome window and play a lofi video on YouTube", "Prepare the dev environment for the project",
                              "Run the tests and fix failures", "Open the docs in Chrome"])
        steps = random.randint(4, 14)
        outcome = random.choices(["finished", "confirmed by the user", "stuck", "step limit"], [60, 20, 14, 6])[0]
        cursor, s_before = at, len(self.spans)
        for i in range(steps):
            kind_ = random.choice(["look", "click", "launch", "run_command", "read_file", "wait", "type_text", "edit_file"])
            verdict = random.choices(["allowed", "approved", "declined", "denied", "invalid"], [78, 10, 3, 4, 5])[0]
            ms = gauss_ms(5500)
            self.span(trace_id, "step", kind_, cursor, ms, status="ok" if verdict in ("allowed", "approved") else "error", verdict=verdict,
                      action={"type": kind_}, observation="synthetic", reasoning="synthetic", result="ok")
            cursor += timedelta(milliseconds=ms)
        for _ in range(steps):
            self.span(trace_id, "llm", "operator", at, gauss_ms(3800), model=MAIN, tin=random.randint(3000, 4200), tout=random.randint(150, 400),
                      purpose="operator", agent="")
        mine = self.spans[s_before:]
        ok = outcome in ("finished", "confirmed by the user")
        self.traces.append(ObsTrace(
            id=trace_id, kind="operator", source="operator", started_at=at, duration_ms=int((cursor - at).total_seconds() * 1000),
            status="ok" if ok else "error", error=None if ok else f"{outcome}: stopped without a result", user_text=goal,
            reply_text="synthetic summary", input_tokens=sum(s.input_tokens for s in mine), output_tokens=sum(s.output_tokens for s in mine),
            llm_calls=steps, cost_usd=sum(s.cost_usd for s in mine),
            meta={"synthetic": True, "stopped_because": outcome, "success": ok, "steps": steps, "run_status": "finished"},
        ))


async def clear() -> None:
    async with session_scope() as session:
        a = await session.execute(delete(ObsTrace).where(text("meta->>'synthetic' = 'true'")))
        b = await session.execute(delete(ObsSpan).where(text("attrs->>'synthetic' = 'true'")))
    print(f"removed {a.rowcount} synthetic traces and {b.rowcount} spans")


async def seed(hours: float, per_hour: float) -> None:
    random.seed(7)
    async with session_scope() as session:
        rows = (await session.execute(select(Memory.id, Memory.content, Memory.kind, Memory.importance).where(Memory.is_active.is_(True)).limit(60))).all()
    builder = Builder([tuple(r) for r in rows])
    now = datetime.now(timezone.utc)
    moment = now - timedelta(hours=hours)
    while moment < now:
        weight = traffic_weight(moment)
        for _ in range(int(random.gauss(per_hour * weight, 1.0) + 0.5)):
            at = moment + timedelta(seconds=random.uniform(0, 3600))
            if at < now:
                builder.chat(at, random.choices(["telegram", "web", "api"], [70, 25, 5])[0])
        if random.random() < 0.12 * weight:
            builder.operator_run(moment + timedelta(seconds=random.uniform(0, 3000)))
        moment += timedelta(hours=1)
    async with session_scope() as session:
        session.add_all(builder.traces)
        await session.flush()
        session.add_all(builder.spans)
    print(f"added {len(builder.traces)} traces and {len(builder.spans)} spans over {hours:g} h (marked synthetic; --clear removes them)")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=72)
    parser.add_argument("--per-hour", type=float, default=6, help="average chats per hour at peak traffic")
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    try:
        await (clear() if args.clear else seed(args.hours, args.per_hour))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
