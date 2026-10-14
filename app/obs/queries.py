"""Everything the dashboard reads. Plain SQL over obs_traces / obs_spans plus the memory tables.

Each function returns JSON-ready dicts. Time ranges are given in hours; series are bucketed so a chart
always has about 120 points, and empty buckets are zero-filled so lines do not skip gaps.
"""

import math
import time
from datetime import date, time as dtime
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.config import settings
from app.core.db import session_scope

BUCKETS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400]
LATENCY_EDGES = [250, 500, 1000, 2000, 3000, 5000, 8000, 12000, 20000, 30000, 60000]


# ---------------------------------------------------------------- helpers
async def rows(sql: str, **params) -> list[dict]:
    async with session_scope() as session:
        result = await session.execute(text(sql), params)
        return [dict(r) for r in result.mappings().all()]


async def one(sql: str, **params) -> dict:
    found = await rows(sql, **params)
    return found[0] if found else {}


def num(value, digits: int | None = None):
    """Decimal / None -> float, so every number in a response is a plain JSON number."""
    if value is None:
        return 0
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return 0
    if digits is not None:
        value = round(value, digits)
    return int(value) if value.is_integer() else value


def window(hours: float) -> tuple[datetime, datetime, int]:
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    target = hours * 3600 / 120
    bucket = next((b for b in BUCKETS if b >= target), BUCKETS[-1])
    return since, until, bucket


GAP_KEYS = {"p50", "p95", "p99"}  # percentiles of nothing are not zero: they stay empty so charts show a gap


def zero_fill(data: list[dict], since: datetime, until: datetime, bucket: int, keys: list[str]) -> list[dict]:
    """One entry per bucket from `since` to `until`; buckets with no data get zeros (None for percentiles)."""
    by_time = {int(r["t"].timestamp()): r for r in data}
    start = int(since.timestamp()) // bucket * bucket
    out = []
    for t in range(start, int(until.timestamp()) + 1, bucket):
        r = by_time.get(t)
        out.append({"t": t * 1000, **{k: num(r[k], 2) if r else (None if k in GAP_KEYS else 0) for k in keys}})
    return out


def fill_days(data: list[dict], days: int, keys: list[str]) -> list[dict]:
    """One entry per UTC day for the last `days` days (today included), zeros where nothing happened."""
    by_day = {r["t"].date(): r for r in data}
    today = datetime.now(timezone.utc).date()
    out = []
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        r = by_day.get(day)
        stamp = int(datetime.combine(day, dtime.min, tzinfo=timezone.utc).timestamp() * 1000)
        out.append({"t": stamp, **{k: num(r[k], 5) if r else 0 for k in keys}})
    return out


BUCKET_SQL = "to_timestamp(floor(extract(epoch FROM started_at) / :bucket) * :bucket)"


# ---------------------------------------------------------------- overview
KPI_SQL = """
SELECT count(*) AS n,
       count(*) FILTER (WHERE status = 'error') AS errors,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) AS p99,
       coalesce(sum(input_tokens), 0) AS tokens_in, coalesce(sum(output_tokens), 0) AS tokens_out,
       coalesce(sum(cost_usd), 0) AS cost, coalesce(sum(llm_calls), 0) AS llm_calls,
       coalesce(sum(tool_calls), 0) AS tool_calls, coalesce(avg(recalled), 0) AS recalled
FROM obs_traces WHERE kind = :kind AND started_at >= :a AND started_at < :b
"""


async def _kpis(kind: str, a: datetime, b: datetime) -> dict:
    r = await one(KPI_SQL, kind=kind, a=a, b=b)
    n = num(r.get("n"))
    return {
        "requests": n, "errors": num(r.get("errors")),
        "error_rate": round(num(r.get("errors")) / n, 4) if n else 0,
        "p50": num(r.get("p50")), "p95": num(r.get("p95")), "p99": num(r.get("p99")),
        "tokens_in": num(r.get("tokens_in")), "tokens_out": num(r.get("tokens_out")),
        "cost": num(r.get("cost"), 5), "llm_calls": num(r.get("llm_calls")), "tool_calls": num(r.get("tool_calls")),
        "recalled": num(r.get("recalled"), 2),
    }


async def overview(hours: float = 24) -> dict:
    since, until, bucket = window(hours)
    previous = since - (until - since)
    current = await _kpis("chat", since, until)
    before = await _kpis("chat", previous, since)
    series = await rows(
        f"""SELECT {BUCKET_SQL} AS t, count(*) AS requests,
                   count(*) FILTER (WHERE status = 'error') AS errors,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                   coalesce(sum(input_tokens), 0) AS tokens_in, coalesce(sum(output_tokens), 0) AS tokens_out,
                   coalesce(sum(cost_usd), 0) AS cost
            FROM obs_traces WHERE kind = 'chat' AND started_at >= :since GROUP BY 1 ORDER BY 1""",
        since=since, bucket=bucket,
    )
    spans = await one(
        """SELECT count(*) FILTER (WHERE kind = 'llm') AS llm, count(*) FILTER (WHERE kind = 'tool') AS tool,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY (attrs->>'ttft_ms')::float)
                      FILTER (WHERE kind = 'llm' AND attrs->>'ttft_ms' IS NOT NULL) AS ttft_p50
           FROM obs_spans WHERE started_at >= :since""",
        since=since,
    )
    by_source = await rows(
        "SELECT source, count(*) AS n FROM obs_traces WHERE kind = 'chat' AND started_at >= :since GROUP BY 1 ORDER BY 2 DESC",
        since=since,
    )
    top_errors = await rows(
        """SELECT kind, name, coalesce(attrs->>'error_type', 'error') AS error_type,
                  left(coalesce(error, ''), 160) AS message, count(*) AS n, max(started_at) AS last_seen
           FROM obs_spans WHERE status = 'error' AND started_at >= :since AND kind IN ('llm', 'tool', 'embed', 'retrieval', 'recall')
           GROUP BY 1, 2, 3, 4 ORDER BY n DESC, last_seen DESC LIMIT 8""",
        since=since,
    )
    slowest = await rows(
        """SELECT id, started_at, duration_ms, status, left(coalesce(user_text, ''), 90) AS user_text, llm_calls, tool_calls
           FROM obs_traces WHERE kind = 'chat' AND started_at >= :since ORDER BY duration_ms DESC LIMIT 6""",
        since=since,
    )
    background = await rows(
        """SELECT kind, count(*) AS runs, count(*) FILTER (WHERE status = 'error') AS errors,
                  coalesce(sum(cost_usd), 0) AS cost
           FROM obs_traces WHERE kind <> 'chat' AND started_at >= :since GROUP BY 1""",
        since=since,
    )
    from app.obs import trace as obs

    return {
        "hours": hours, "bucket": bucket, "current": current, "previous": before,
        "series": zero_fill(series, since, until, bucket, ["requests", "errors", "p50", "p95", "tokens_in", "tokens_out", "cost"]),
        "llm_spans": num(spans.get("llm")), "tool_spans": num(spans.get("tool")), "ttft_p50": num(spans.get("ttft_p50")),
        "by_source": [{"source": r["source"] or "?", "n": num(r["n"])} for r in by_source],
        "top_errors": [{**r, "n": num(r["n"])} for r in top_errors],
        "slowest": slowest,
        "background": [{**r, "runs": num(r["runs"]), "errors": num(r["errors"]), "cost": num(r["cost"], 5)} for r in background],
        "in_flight": [
            {"id": str(t.id), "kind": t.kind, "source": t.source, "age_ms": round((time.perf_counter() - t.t0) * 1000),
             "spans": len(t.spans), "text": (t.user_text or "")[:100]}
            for t in obs.ACTIVE.values()
        ],
    }


# ---------------------------------------------------------------- traces
async def traces(kind: str = "", status: str = "", source: str = "", q: str = "", min_ms: int = 0,
                 hours: float = 24, limit: int = 50, offset: int = 0) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    where = ["started_at >= :since", "duration_ms >= :min_ms"]
    params: dict = {"since": since, "min_ms": min_ms, "limit": limit, "offset": offset}
    for column, value in (("kind", kind), ("status", status), ("source", source)):
        if value:
            where.append(f"{column} = :{column}")
            params[column] = value
    if q:
        where.append("(user_text ILIKE :q OR reply_text ILIKE :q OR error ILIKE :q)")
        params["q"] = f"%{q}%"
    clause = " AND ".join(where)
    total = await one(f"SELECT count(*) AS n FROM obs_traces WHERE {clause}", **params)
    data = await rows(
        f"""SELECT id, kind, source, started_at, duration_ms, status, left(coalesce(error, ''), 200) AS error,
                   left(coalesce(user_text, ''), 160) AS user_text, left(coalesce(reply_text, ''), 160) AS reply_text,
                   input_tokens, output_tokens, llm_calls, tool_calls, cost_usd, recalled
            FROM obs_traces WHERE {clause} ORDER BY started_at DESC LIMIT :limit OFFSET :offset""",
        **params,
    )
    return {"total": num(total.get("n")), "rows": data}


async def trace_detail(trace_id) -> dict | None:
    head = await one("SELECT * FROM obs_traces WHERE id = :id", id=trace_id)
    if not head:
        return None
    spans = await rows(
        """SELECT id, kind, name, started_at, duration_ms, status, error, model, input_tokens, output_tokens, cost_usd, attrs
           FROM obs_spans WHERE trace_id = :id ORDER BY started_at, duration_ms DESC""",
        id=trace_id,
    )
    t0 = head["started_at"]
    for s in spans:
        s["offset_ms"] = max(0, round((s["started_at"] - t0).total_seconds() * 1000))
    return {"trace": head, "spans": spans}


# ---------------------------------------------------------------- LLM
async def llm(hours: float = 24) -> dict:
    since, until, bucket = window(hours)
    groups = await rows(
        """SELECT name, coalesce(model, '?') AS model, count(*) AS calls,
                  count(*) FILTER (WHERE status = 'error') AS errors,
                  coalesce(sum(input_tokens), 0) AS tokens_in, coalesce(sum(output_tokens), 0) AS tokens_out,
                  coalesce(sum(cost_usd), 0) AS cost,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                  percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) AS p99,
                  avg((attrs->>'ttft_ms')::float) FILTER (WHERE attrs->>'ttft_ms' IS NOT NULL) AS ttft,
                  avg(input_tokens) AS avg_in, avg(output_tokens) AS avg_out, max(input_tokens) AS max_in
           FROM obs_spans WHERE kind = 'llm' AND started_at >= :since GROUP BY 1, 2 ORDER BY cost DESC, calls DESC""",
        since=since,
    )
    series = await rows(
        f"""SELECT {BUCKET_SQL} AS t, count(*) AS calls, count(*) FILTER (WHERE status = 'error') AS errors,
                   coalesce(sum(input_tokens), 0) AS tokens_in, coalesce(sum(output_tokens), 0) AS tokens_out,
                   coalesce(sum(cost_usd), 0) AS cost,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95
            FROM obs_spans WHERE kind = 'llm' AND started_at >= :since GROUP BY 1 ORDER BY 1""",
        since=since, bucket=bucket,
    )
    edges = ", ".join(str(e) for e in LATENCY_EDGES)
    hist = await rows(
        f"""SELECT width_bucket(duration_ms::float8, ARRAY[{edges}]::float8[]) AS b, count(*) AS n
            FROM obs_spans WHERE kind = 'llm' AND started_at >= :since GROUP BY 1 ORDER BY 1""",
        since=since,
    )
    counts = {int(r["b"]): num(r["n"]) for r in hist}
    labels = [f"<{LATENCY_EDGES[0] / 1000:g}s"] + [
        f"{LATENCY_EDGES[i] / 1000:g}-{LATENCY_EDGES[i + 1] / 1000:g}s" for i in range(len(LATENCY_EDGES) - 1)
    ] + [f">{LATENCY_EDGES[-1] / 1000:g}s"]
    errors = await rows(
        """SELECT coalesce(attrs->>'error_type', 'error') AS error_type, name, left(coalesce(error, ''), 200) AS message,
                  count(*) AS n, max(started_at) AS last_seen
           FROM obs_spans WHERE kind = 'llm' AND status = 'error' AND started_at >= :since
           GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 12""",
        since=since,
    )
    embeds = await one(
        """SELECT count(*) AS calls, count(*) FILTER (WHERE status = 'error') AS errors,
                  coalesce(sum(input_tokens), 0) AS tokens, coalesce(sum(cost_usd), 0) AS cost,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95
           FROM obs_spans WHERE kind = 'embed' AND started_at >= :since""",
        since=since,
    )
    daily = await rows(
        """SELECT date_trunc('day', started_at) AS t, coalesce(sum(cost_usd), 0) AS cost,
                  coalesce(sum(input_tokens), 0) AS tokens_in, coalesce(sum(output_tokens), 0) AS tokens_out
           FROM obs_spans WHERE kind IN ('llm', 'embed') AND started_at >= now() - interval '30 days'
           GROUP BY 1 ORDER BY 1""",
    )
    recent = await rows(
        """SELECT id, trace_id, name, model, started_at, duration_ms, status, input_tokens, output_tokens, cost_usd,
                  left(coalesce(error, ''), 160) AS error, attrs->>'ttft_ms' AS ttft, attrs->>'purpose' AS purpose,
                  left(coalesce(attrs->>'last_input', ''), 140) AS last_input, left(coalesce(attrs->>'output', ''), 140) AS output
           FROM obs_spans WHERE kind = 'llm' AND started_at >= :since ORDER BY started_at DESC LIMIT 40""",
        since=since,
    )
    total_cost = sum(num(g["cost"], 6) for g in groups)
    return {
        "hours": hours, "bucket": bucket,
        "groups": [
            {**{k: g[k] for k in ("name", "model")}, "calls": num(g["calls"]), "errors": num(g["errors"]),
             "tokens_in": num(g["tokens_in"]), "tokens_out": num(g["tokens_out"]), "cost": num(g["cost"], 5),
             "p50": num(g["p50"]), "p95": num(g["p95"]), "p99": num(g["p99"]), "ttft": num(g["ttft"]),
             "avg_in": num(g["avg_in"]), "avg_out": num(g["avg_out"]), "max_in": num(g["max_in"])}
            for g in groups
        ],
        "total_cost": round(total_cost, 5),
        "series": zero_fill(series, since, until, bucket, ["calls", "errors", "tokens_in", "tokens_out", "cost", "p95"]),
        "histogram": [{"label": labels[i], "n": counts.get(i, 0)} for i in range(len(labels))],
        "errors": [{**e, "n": num(e["n"])} for e in errors],
        "embeddings": {k: num(v, 5) for k, v in embeds.items()},
        "daily": fill_days(daily, 30, ["cost", "tokens_in", "tokens_out"]),
        "recent": recent,
    }


# ---------------------------------------------------------------- tools
async def tools(hours: float = 24) -> dict:
    since, until, bucket = window(hours)
    stats = await rows(
        """SELECT name, count(*) AS calls, count(*) FILTER (WHERE status = 'error') AS errors,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                  max(duration_ms) AS max_ms, max(started_at) AS last_call,
                  max(started_at) FILTER (WHERE status = 'error') AS last_error_at
           FROM obs_spans WHERE kind = 'tool' AND started_at >= :since GROUP BY 1 ORDER BY calls DESC""",
        since=since,
    )
    series = await rows(
        f"""SELECT {BUCKET_SQL} AS t, count(*) AS calls, count(*) FILTER (WHERE status = 'error') AS errors
            FROM obs_spans WHERE kind = 'tool' AND started_at >= :since GROUP BY 1 ORDER BY 1""",
        since=since, bucket=bucket,
    )
    recent = await rows(
        """SELECT id, trace_id, name, started_at, duration_ms, status, left(coalesce(error, ''), 240) AS error,
                  attrs->>'args' AS args, attrs->>'result' AS result, attrs->>'agent' AS agent
           FROM obs_spans WHERE kind = 'tool' AND started_at >= :since ORDER BY started_at DESC LIMIT 60""",
        since=since,
    )
    return {
        "hours": hours, "bucket": bucket,
        "stats": [{**s, "calls": num(s["calls"]), "errors": num(s["errors"]), "p50": num(s["p50"]), "p95": num(s["p95"]),
                   "max_ms": num(s["max_ms"])} for s in stats],
        "series": zero_fill(series, since, until, bucket, ["calls", "errors"]),
        "recent": recent,
    }


# ---------------------------------------------------------------- retrieval
async def retrieval(hours: float = 24) -> dict:
    since, until, bucket = window(hours)
    recall = await one(
        """SELECT count(*) AS n, count(*) FILTER (WHERE (attrs->>'needs_memory') = 'false') AS skipped,
                  count(*) FILTER (WHERE status = 'error') AS errors
           FROM obs_spans WHERE kind = 'recall' AND started_at >= :since""",
        since=since,
    )
    search = await one(
        """SELECT count(*) AS n, count(*) FILTER (WHERE (attrs->>'returned')::int = 0) AS empty,
                  count(*) FILTER (WHERE status = 'error') AS errors,
                  avg((attrs->>'candidates')::int) AS candidates, avg((attrs->>'returned')::int) AS returned,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                  count(*) FILTER (WHERE (attrs->>'reranked') = 'true') AS reranked
           FROM obs_spans WHERE kind = 'retrieval' AND started_at >= :since""",
        since=since,
    )
    channels = await rows(
        """SELECT c.key AS channel, count(*) AS used, avg((c.value->>'hits')::int) AS avg_hits,
                  count(*) FILTER (WHERE (c.value->>'hits')::int = 0) AS zero,
                  avg((c.value->>'ms')::float) AS avg_ms,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY (c.value->>'ms')::float) AS p95_ms,
                  count(*) FILTER (WHERE c.value->>'error' IS NOT NULL) AS errors
           FROM obs_spans s, jsonb_each(s.attrs->'channels') AS c
           WHERE s.kind = 'retrieval' AND s.started_at >= :since AND jsonb_typeof(s.attrs->'channels') = 'object'
           GROUP BY 1 ORDER BY used DESC""",
        since=since,
    )
    series = await rows(
        f"""SELECT {BUCKET_SQL} AS t, count(*) AS searches, count(*) FILTER (WHERE (attrs->>'returned')::int = 0) AS empty,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95
            FROM obs_spans WHERE kind = 'retrieval' AND started_at >= :since GROUP BY 1 ORDER BY 1""",
        since=since, bucket=bucket,
    )
    sample = await rows(
        """SELECT id, trace_id, started_at, duration_ms, status, error, attrs
           FROM obs_spans WHERE kind = 'retrieval' AND started_at >= :since ORDER BY started_at DESC LIMIT 600""",
        since=since,
    )

    # Aggregates that need the per-result detail are done here over the most recent searches.
    contribution = defaultdict(lambda: {"results": 0, "unique": 0, "rank_sum": 0, "rank1": 0})
    plan_use = Counter()
    kinds, importances, hot = Counter(), Counter(), Counter()
    scores = []
    moved = []
    for r in sample:
        attrs = r["attrs"] or {}
        plan = attrs.get("plan") or {}
        for field in ("keywords", "semantic_query", "entities", "relation_words"):
            if plan.get(field):
                plan_use[field] += 1
        if plan.get("start") or plan.get("end"):
            plan_use["time_window"] += 1
        for item in attrs.get("results") or []:
            found = item.get("found_by") or {}
            for channel, position in found.items():
                c = contribution[channel]
                c["results"] += 1
                c["rank_sum"] += position
                c["rank1"] += position == 1
                c["unique"] += len(found) == 1
            kinds[item.get("kind")] += 1
            importances[item.get("importance")] += 1
            hot[item["id"]] += 1
            scores.append(item.get("score", 0))
            if item.get("moved_by_rerank"):
                moved.append(item["moved_by_rerank"])

    hot_ids = [i for i, _ in hot.most_common(10)]
    hot_rows = {}
    if hot_ids:
        found_rows = await rows(
            "SELECT id::text AS id, kind, importance, is_active, left(content, 220) AS content FROM memories WHERE id::text = ANY(:ids)",
            ids=hot_ids,
        )
        hot_rows = {r["id"]: r for r in found_rows}
    pool = await one("SELECT count(*) FILTER (WHERE is_active) AS active FROM memories")
    recalled_distinct = len(hot)

    edges = [0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 1]
    score_hist = Counter()
    for s in scores:
        score_hist[next(i for i in range(len(edges) - 1) if s < edges[i + 1] or i == len(edges) - 2)] += 1

    latest = []
    for r in sample[:25]:
        attrs = r["attrs"] or {}
        plan = attrs.get("plan") or {}
        query = plan.get("semantic_query") or ", ".join(plan.get("keywords") or []) or ", ".join(plan.get("entities") or []) \
            or ", ".join(plan.get("relation_words") or []) or ("(time window)" if plan.get("start") else "(empty plan)")
        latest.append({
            "id": str(r["id"]), "trace_id": str(r["trace_id"]) if r["trace_id"] else None, "started_at": r["started_at"],
            "duration_ms": r["duration_ms"], "status": r["status"], "query": query, "plan": plan,
            "channels": attrs.get("channels") or {}, "candidates": attrs.get("candidates", 0),
            "returned": attrs.get("returned", 0), "reranked": attrs.get("reranked", False),
            "results": attrs.get("results") or [], "error": r["error"],
        })

    searches = num(search.get("n"))
    return {
        "hours": hours, "bucket": bucket,
        "recall": {"requests": num(recall.get("n")), "skipped": num(recall.get("skipped")), "errors": num(recall.get("errors"))},
        "search": {
            "searches": searches, "empty": num(search.get("empty")), "errors": num(search.get("errors")),
            "empty_rate": round(num(search.get("empty")) / searches, 4) if searches else 0,
            "avg_candidates": num(search.get("candidates"), 1), "avg_returned": num(search.get("returned"), 1),
            "p50": num(search.get("p50")), "p95": num(search.get("p95")), "reranked": num(search.get("reranked")),
        },
        "channels": [
            {"channel": c["channel"], "used": num(c["used"]), "avg_hits": num(c["avg_hits"], 1),
             "zero_rate": round(num(c["zero"]) / num(c["used"]), 3) if num(c["used"]) else 0,
             "avg_ms": num(c["avg_ms"]), "p95_ms": num(c["p95_ms"]), "errors": num(c["errors"]),
             "results": contribution[c["channel"]]["results"], "unique": contribution[c["channel"]]["unique"],
             "rank1": contribution[c["channel"]]["rank1"],
             "avg_rank": round(contribution[c["channel"]]["rank_sum"] / contribution[c["channel"]]["results"], 2)
             if contribution[c["channel"]]["results"] else 0}
            for c in channels
        ],
        "plan_use": dict(plan_use),
        "kinds": {str(k): v for k, v in kinds.items()},
        "importances": {str(k): v for k, v in sorted(importances.items(), key=lambda kv: (kv[0] is None, kv[0]))},
        "score_hist": [{"label": f"{edges[i]:g}-{edges[i + 1]:g}", "n": score_hist.get(i, 0)} for i in range(len(edges) - 1)],
        "rerank_moves": {"count": len(moved), "avg_abs": round(sum(abs(m) for m in moved) / len(moved), 2) if moved else 0},
        "series": zero_fill(series, since, until, bucket, ["searches", "empty", "p50", "p95"]),
        "coverage": {"distinct_recalled": recalled_distinct, "active_memories": num(pool.get("active")),
                     "searches_sampled": len(sample)},
        "hot_memories": [
            {"id": i, "times": hot[i], **{k: v for k, v in (hot_rows.get(i) or {"content": "(deleted)"}).items() if k != "id"}}
            for i in hot_ids
        ],
        "latest": latest,
    }


# ---------------------------------------------------------------- memory
async def memory() -> dict:
    from app.memory.maintenance import memory_health

    totals = await one(
        """SELECT count(*) AS total, count(*) FILTER (WHERE is_active) AS active,
                  count(*) FILTER (WHERE NOT is_active AND superseded_by_id IS NOT NULL) AS superseded,
                  count(*) FILTER (WHERE NOT is_active AND superseded_by_id IS NULL) AS forgotten,
                  count(*) FILTER (WHERE is_active AND embedding IS NOT NULL) AS embedded,
                  count(*) FILTER (WHERE is_active AND wants_embedding) AS wants_embedding,
                  count(*) FILTER (WHERE is_active AND event_at IS NOT NULL) AS dated,
                  avg(length(content)) FILTER (WHERE is_active) AS avg_len,
                  count(*) FILTER (WHERE is_active AND NOT EXISTS (SELECT 1 FROM memory_entities me WHERE me.memory_id = memories.id)) AS unlinked
           FROM memories"""
    )
    by_kind = await rows("SELECT kind, count(*) AS n FROM memories WHERE is_active GROUP BY 1 ORDER BY 2 DESC")
    by_importance = await rows("SELECT importance, count(*) AS n FROM memories WHERE is_active GROUP BY 1 ORDER BY 1")
    growth = await rows(
        """SELECT date_trunc('day', created_at) AS t, count(*) AS created FROM memories
           WHERE created_at >= date_trunc('day', now()) - interval '29 days' GROUP BY 1 ORDER BY 1"""
    )
    before_window = await one("SELECT count(*) AS n FROM memories WHERE is_active AND created_at < date_trunc('day', now()) - interval '29 days'")
    entities = await rows("SELECT type, count(*) AS n FROM entities GROUP BY 1 ORDER BY 2 DESC")
    relations = await one("SELECT count(*) AS n, count(DISTINCT predicate) AS predicates FROM entity_relations")
    top_entities = await rows(
        """SELECT e.name, e.type, count(me.memory_id) AS memories,
                  (SELECT count(*) FROM entity_relations r WHERE r.subject_id = e.id OR r.target_id = e.id) AS relations
           FROM entities e LEFT JOIN memory_entities me ON me.entity_id = e.id
           GROUP BY e.id, e.name, e.type ORDER BY memories DESC, relations DESC LIMIT 12"""
    )
    extraction = await one(
        """SELECT count(*) AS runs, count(*) FILTER (WHERE status = 'error') AS errors,
                  coalesce(avg((meta->>'proposed')::int), 0) AS proposed, coalesce(avg((meta->>'saved')::int), 0) AS saved,
                  count(*) FILTER (WHERE (meta->>'saved')::int = 0) AS empty, coalesce(sum(cost_usd), 0) AS cost,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50
           FROM obs_traces WHERE kind = 'extraction' AND started_at >= now() - interval '7 days'"""
    )
    extraction_daily = await rows(
        """SELECT date_trunc('day', started_at) AS t, count(*) AS runs, coalesce(sum((meta->>'saved')::int), 0) AS saved,
                  count(*) FILTER (WHERE status = 'error') AS errors
           FROM obs_traces WHERE kind = 'extraction' AND started_at >= now() - interval '14 days' GROUP BY 1 ORDER BY 1"""
    )
    recent = await rows(
        """SELECT id::text AS id, kind, importance, is_active, wants_embedding, embedding IS NOT NULL AS embedded,
                  left(content, 240) AS content, created_at, event_at
           FROM memories ORDER BY created_at DESC LIMIT 15"""
    )
    chat = await one(
        """SELECT (SELECT count(*) FROM conversations) AS conversations, (SELECT count(*) FROM messages) AS messages,
                  (SELECT avg(length(content)) FROM messages WHERE role = 'user') AS avg_user_len,
                  (SELECT avg(length(content)) FROM messages WHERE role = 'assistant') AS avg_reply_len,
                  (SELECT count(*) FROM tasks WHERE status = 'open') AS tasks_open,
                  (SELECT count(*) FROM tasks WHERE status <> 'open') AS tasks_done,
                  (SELECT count(*) FROM notes) AS notes,
                  (SELECT length(profile) FROM users LIMIT 1) AS profile_chars,
                  (SELECT profile_updated_at FROM users LIMIT 1) AS profile_updated_at"""
    )
    messages_daily = await rows(
        "SELECT date_trunc('day', created_at) AS t, count(*) AS n FROM messages WHERE created_at >= now() - interval '14 days' GROUP BY 1 ORDER BY 1"
    )
    health = await memory_health()

    # cumulative active count per day, anchored on what existed before the window
    running = num(before_window.get("n"))
    cumulative = []
    for g in fill_days(growth, 30, ["created"]):
        running += g["created"]
        cumulative.append({**g, "total": running})

    runs = num(extraction.get("runs"))
    return {
        "totals": {k: num(v, 1) for k, v in totals.items()},
        "by_kind": [{"kind": r["kind"], "n": num(r["n"])} for r in by_kind],
        "by_importance": [{"importance": r["importance"], "n": num(r["n"])} for r in by_importance],
        "growth": cumulative,
        "entities": [{"type": r["type"], "n": num(r["n"])} for r in entities],
        "relations": {k: num(v) for k, v in relations.items()},
        "top_entities": [{**r, "memories": num(r["memories"]), "relations": num(r["relations"])} for r in top_entities],
        "extraction": {
            "runs": runs, "errors": num(extraction.get("errors")), "avg_proposed": num(extraction.get("proposed"), 2),
            "avg_saved": num(extraction.get("saved"), 2), "empty_rate": round(num(extraction.get("empty")) / runs, 3) if runs else 0,
            "cost": num(extraction.get("cost"), 5), "p50": num(extraction.get("p50")),
            "daily": fill_days(extraction_daily, 14, ["runs", "saved", "errors"]),
        },
        "recent": recent,
        "chat": {k: (v if v is None or hasattr(v, "isoformat") else num(v, 1)) for k, v in chat.items()},
        "messages_daily": fill_days(messages_daily, 14, ["n"]),
        "health": health,
    }


# ---------------------------------------------------------------- raw database browser
BROWSABLE = ["memories", "entities", "entity_relations", "memory_entities", "conversations", "messages",
             "tasks", "notes", "users", "obs_traces", "obs_spans"]
DEFAULT_SORT = {"memories": "created_at", "entities": "created_at", "conversations": "created_at",
                "messages": "created_at", "tasks": "created_at", "notes": "created_at", "users": "created_at",
                "obs_traces": "started_at", "obs_spans": "started_at"}
CELL_LIMIT = 4000


async def db_tables() -> list[dict]:
    out = []
    for name in BROWSABLE:
        r = await one(f"SELECT count(*) AS n, pg_total_relation_size('{name}') AS bytes FROM {name}")  # name is from BROWSABLE
        out.append({"name": name, "rows": num(r.get("n")), "bytes": num(r.get("bytes"))})
    return out


def _cell(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        import json

        value = json.dumps(value, default=str)
    value = str(value)
    return value if len(value) <= CELL_LIMIT else value[:CELL_LIMIT] + "..."


async def db_rows(table: str, limit: int = 50, offset: int = 0, q: str = "", sort: str = "", desc: bool = True) -> dict | None:
    from app.core.models import Base

    if table not in BROWSABLE:
        return None
    model_table = Base.metadata.tables[table]
    columns = [c.name for c in model_table.columns if c.name != "embedding"]
    if sort not in columns:
        sort = DEFAULT_SORT.get(table) or columns[0]
    select_list = ", ".join(f'"{c}"' for c in columns)
    if table == "memories":
        select_list += ", (embedding IS NOT NULL) AS has_embedding"
        columns = columns + ["has_embedding"]
    where, params = "", {"limit": min(limit, 200), "offset": offset}
    if q:
        where = "WHERE " + " OR ".join(f'"{c}"::text ILIKE :q' for c in columns if c != "has_embedding")
        params["q"] = f"%{q}%"
    total = await one(f"SELECT count(*) AS n FROM {table} {where}", **{k: v for k, v in params.items() if k == "q"})
    order = f'ORDER BY "{sort}" {"DESC" if desc else "ASC"}'
    data = await rows(f"SELECT {select_list} FROM {table} {where} {order} LIMIT :limit OFFSET :offset", **params)
    return {
        "table": table, "columns": columns, "sort": sort, "total": num(total.get("n")),
        "rows": [{c: _cell(r.get(c)) for c in columns} for r in data],
    }


# ---------------------------------------------------------------- system
async def ping_db() -> float:
    t0 = time.perf_counter()
    await one("SELECT 1 AS ok")
    return round((time.perf_counter() - t0) * 1000, 1)
