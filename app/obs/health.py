"""Live health signals (event-loop lag, process facts), alert rules, retention, and the /obs/system snapshot."""

import asyncio
import logging
import os
import platform
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.obs import logs, queries
from app.obs import trace as obs

log = logging.getLogger(__name__)

STARTED_AT = datetime.now(timezone.utc)
LAG_SAMPLES: deque[tuple[float, float]] = deque(maxlen=720)  # (epoch seconds, lag in ms): about 6 minutes at 0.5s
LAG_INTERVAL = 0.5
PURGE_EVERY = 6 * 3600


async def sample_loop_lag() -> None:
    """How late does the event loop wake a 0.5 s sleep? Persistent lag means something is blocking it."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(LAG_INTERVAL)
        LAG_SAMPLES.append((time.time(), max(0.0, (time.perf_counter() - t0 - LAG_INTERVAL) * 1000)))


async def purge_old() -> int:
    """Delete traces (and, by cascade, their spans) older than the retention period, plus loose spans."""
    days = settings.telemetry_retention_days
    deleted = await queries.one(
        """WITH t AS (DELETE FROM obs_traces WHERE started_at < now() - make_interval(days => :days) RETURNING 1),
                s AS (DELETE FROM obs_spans WHERE trace_id IS NULL AND started_at < now() - make_interval(days => :days) RETURNING 1)
           SELECT (SELECT count(*) FROM t) AS traces, (SELECT count(*) FROM s) AS spans""",
        days=days,
    )
    return int(deleted.get("traces", 0)) + int(deleted.get("spans", 0))


async def purge_loop() -> None:
    while True:
        try:
            removed = await purge_old()
            if removed:
                log.info("telemetry retention: removed %d old rows", removed)
        except Exception:
            log.warning("telemetry retention failed", exc_info=True)
        await asyncio.sleep(PURGE_EVERY)


def lag_stats() -> dict:
    values = sorted(v for _, v in LAG_SAMPLES)
    if not values:
        return {"samples": 0, "p50": 0, "p95": 0, "max": 0, "series": []}
    pick = lambda q: round(values[min(len(values) - 1, int(len(values) * q))], 1)
    recent = list(LAG_SAMPLES)[-120:]
    return {"samples": len(values), "p50": pick(0.5), "p95": pick(0.95), "max": round(values[-1], 1),
            "series": [{"t": int(t * 1000), "v": round(v, 1)} for t, v in recent]}


def _process_memory_mb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            kernel, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            kernel.GetCurrentProcess.restype = wintypes.HANDLE  # a 64-bit handle must not be truncated to int
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / 1e6, 1)
            return None
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(rss / (1e6 if sys.platform == "darwin" else 1e3), 1)
    except Exception:
        return None


async def system() -> dict:
    from app import telegram_bot
    from app.core.db import engine

    ping = await queries.ping_db()
    sizes = await queries.one(
        "SELECT pg_database_size(current_database()) AS db_bytes, (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector, version() AS pg"
    )
    pool = engine.pool
    try:
        pool_info = {"size": pool.size(), "checked_out": pool.checkedout()}
    except Exception:
        pool_info = {}
    bot = telegram_bot.instance
    return {
        "started_at": STARTED_AT, "uptime_s": round((datetime.now(timezone.utc) - STARTED_AT).total_seconds()),
        "pid": os.getpid(), "python": platform.python_version(), "platform": platform.platform(),
        "process_mb": _process_memory_mb(), "db_ping_ms": ping, "pool": pool_info,
        "database": {"bytes": queries.num(sizes.get("db_bytes")), "pgvector": sizes.get("pgvector"), "version": (sizes.get("pg") or "")[:60]},
        "loop_lag": lag_stats(),
        "log_counts": logs.handler.counts(),
        "telegram": bot.stats() if bot else {"running": False},
        "config": {
            "main_model": settings.main_model_id, "fast_model": settings.fast_model_id, "embedding_model": settings.embedding_model_id,
            "embedding_dim": settings.embedding_dim, "aws_region": settings.aws_region, "rerank": settings.rerank,
            "recall_limit": settings.recall_limit, "history_messages": settings.history_messages, "min_similarity": settings.min_similarity,
            "weights": {"keyword": settings.weight_keyword, "semantic": settings.weight_semantic, "graph": settings.weight_graph, "time": settings.weight_time},
            "graph_max_hops": settings.graph_max_hops, "importance_weight": settings.importance_weight,
            "dedup": {"trigram": settings.dedup_trigram_threshold, "vector": settings.dedup_vector_threshold},
            "storage": settings.storage_backend, "timezone": settings.user_timezone,
            "telemetry": {"enabled": settings.telemetry_enabled, "retention_days": settings.telemetry_retention_days,
                          "store_content": settings.telemetry_store_content},
            "api_key_set": bool(settings.api_key), "tavily_set": bool(settings.tavily_api_key),
            "telegram_set": bool(settings.telegram_bot_token),
        },
        "in_flight": len(obs.ACTIVE),
    }


# ---------------------------------------------------------------- alerts
def _alert(alerts: list, key: str, severity: str, title: str, detail: str, value=None) -> None:
    alerts.append({"id": key, "severity": severity, "title": title, "detail": detail, "value": value})


async def alerts() -> list[dict]:
    """Rule-based checks over the last hour (and a few live signals). Ordered worst first."""
    out: list[dict] = []
    now = datetime.now(timezone.utc)
    hour = await queries._kpis("chat", now - timedelta(hours=1), now)
    if hour["requests"] >= 5:
        if hour["error_rate"] > 0.25:
            _alert(out, "chat-errors", "critical", "Chat error rate is high", f"{hour['errors']} of {hour['requests']} turns failed in the last hour", hour["error_rate"])
        elif hour["error_rate"] > 0.08:
            _alert(out, "chat-errors", "warn", "Chat error rate is elevated", f"{hour['errors']} of {hour['requests']} turns failed in the last hour", hour["error_rate"])
        if hour["p95"] > 60000:
            _alert(out, "latency", "critical", "p95 latency above 60 s", f"p95 {hour['p95'] / 1000:.1f}s over {hour['requests']} turns", hour["p95"])
        elif hour["p95"] > 30000:
            _alert(out, "latency", "warn", "p95 latency above 30 s", f"p95 {hour['p95'] / 1000:.1f}s over {hour['requests']} turns", hour["p95"])

    llm_errors = await queries.rows(
        """SELECT coalesce(attrs->>'error_type', 'error') AS error_type, count(*) AS n
           FROM obs_spans WHERE kind = 'llm' AND status = 'error' AND started_at > now() - interval '1 hour' GROUP BY 1 ORDER BY 2 DESC"""
    )
    throttled = sum(r["n"] for r in llm_errors if "Throttl" in r["error_type"])
    if throttled:
        _alert(out, "throttling", "critical", "Bedrock is throttling requests", f"{throttled} throttled model calls in the last hour: slow down or raise the quota", throttled)
    other = sum(r["n"] for r in llm_errors) - throttled
    if other:
        _alert(out, "llm-errors", "warn", "Model calls are failing", ", ".join(f"{r['error_type']} x{r['n']}" for r in llm_errors[:3]), other)

    tool_fail = await queries.rows(
        """SELECT name, count(*) AS n, count(*) FILTER (WHERE status = 'error') AS bad FROM obs_spans
           WHERE kind = 'tool' AND started_at > now() - interval '24 hours' GROUP BY 1 HAVING count(*) >= 4"""
    )
    for t in tool_fail:
        if t["bad"] / t["n"] > 0.25:
            _alert(out, f"tool-{t['name']}", "warn", f"Tool '{t['name']}' keeps failing", f"{t['bad']} of {t['n']} calls failed in 24 h", round(t["bad"] / t["n"], 2))

    search = await queries.one(
        """SELECT count(*) AS n, count(*) FILTER (WHERE (attrs->>'returned')::int = 0) AS empty,
                  count(*) FILTER (WHERE status = 'error') AS errors FROM obs_spans WHERE kind = 'retrieval' AND started_at > now() - interval '24 hours'"""
    )
    if search.get("n", 0) >= 10 and search["empty"] / search["n"] > 0.8:
        _alert(out, "empty-recall", "info", "Most memory searches find nothing", f"{search['empty']} of {search['n']} searches returned 0 memories in 24 h. Is memory filling up?", round(search["empty"] / search["n"], 2))
    channel_errors = await queries.one(
        """SELECT count(*) AS n FROM obs_spans s, jsonb_each(s.attrs->'channels') c WHERE s.kind = 'retrieval' AND s.started_at > now() - interval '24 hours'
           AND jsonb_typeof(s.attrs->'channels') = 'object' AND c.value->>'error' IS NOT NULL"""
    )
    if channel_errors.get("n"):
        _alert(out, "channel-errors", "warn", "A retrieval channel is failing", f"{channel_errors['n']} channel failures in 24 h (see Retrieval)", channel_errors["n"])

    from app.memory.maintenance import memory_health

    health = await memory_health()
    if not health["healthy"]:
        _alert(out, "embeddings", "warn", "Memories are missing current embeddings", f"{health['needs_embedding']} memories need (re-)embedding. They are invisible to semantic search. Run reembed.", health["needs_embedding"])

    extraction = await queries.one(
        "SELECT count(*) AS n, count(*) FILTER (WHERE status = 'error') AS bad FROM obs_traces WHERE kind = 'extraction' AND started_at > now() - interval '24 hours'"
    )
    if extraction.get("bad"):
        _alert(out, "extraction", "warn", "Memory extraction is failing", f"{extraction['bad']} of {extraction['n']} background extractions failed in 24 h: new facts are not being saved", extraction["bad"])

    lag = lag_stats()
    if lag["samples"] > 60 and lag["p95"] > 250:
        _alert(out, "loop-lag", "warn", "The event loop is being blocked", f"p95 wake-up lag {lag['p95']} ms: something synchronous is running on the server", lag["p95"])
    ping = await queries.ping_db()
    if ping > 250:
        _alert(out, "db-slow", "warn", "Database round trip is slow", f"SELECT 1 took {ping} ms", ping)

    cutoff = (now - timedelta(minutes=15)).isoformat()
    recent_errors = sum(1 for r in logs.handler.query("ERROR", limit=500) if r["ts"] > cutoff)
    if recent_errors >= 5:
        _alert(out, "log-errors", "warn", "Error logs are piling up", f"{recent_errors} ERROR lines in the last 15 minutes (see Logs)", recent_errors)
    if not settings.api_key:
        _alert(out, "no-api-key", "info", "API_KEY is not set", "The dashboard and all data endpoints are open to anyone who can reach this server", None)
    order = {"critical": 0, "warn": 1, "info": 2}
    out.sort(key=lambda a: order[a["severity"]])
    return out
