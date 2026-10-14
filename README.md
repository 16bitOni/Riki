# Riki — Personal AI Agent

> A single-user, multi-agent personal assistant that **thinks between conversations**.  
> Not a chatbot with a memory database — a mind that happens to talk through Telegram.

---

## What makes Riki different

Most AI assistants are reactive. You talk, they respond, the lights go off. Riki has **continuity of thought** — it processes new information against old understanding, forms hypotheses, notices patterns, and reaches out when something matters.

**Key engineering decisions behind this:**

| What | Why it matters |
|---|---|
| **Tiered memory** — immutable archive (S3/folder) + queryable index (Postgres) | The archive is the source of truth. The entire index can be rebuilt from it at any time with a better model or better prompts — nothing is ever lost |
| **Selective embeddings** — only preferences and episode summaries are embedded by default | Keeps the vector index small and the embedding bill near zero; exact search, time filters, and the knowledge graph handle names, dates, and places |
| **Embedding drift detection** — every vector is stored with the model name that made it | A model switch can never produce silently wrong matches; a CHECK constraint refuses a vector with no model name |
| **Near-duplicate detection** — trigram similarity first, then vector distance | Two-pass dedup before saving; the older memory is retired and superseded rather than duplicated |
| **Cognition cycle** — runs every ~8 hours in the background | The agent sits with what it knows, tracks threads over days, and decides what deserves attention — the gap between conversations is where thinking happens |
| **Full observability** — every model call, tool call, memory search, and latency percentile is recorded | Live dashboard at `/dashboard`; cost estimates from token counts; zero trust (scrubs tokens and API keys from logs) |

---

## Stack

```
FastAPI · LangGraph · Postgres + pgvector · SQLAlchemy 2.0 (async) · Alembic · Pydantic · AWS Bedrock
```

---

## Architecture

```
  browser / Telegram → POST /chat
                             │
              ┌──────── LangGraph ─────────┐
              │  recall  (memory router)   │──► profile + relevant memories + time context
              │      ▼                     │
              │  supervisor ──► planner    │──► tasks, notes, Google Calendar
              │             ──► researcher │──► web search (Tavily)
              │             ──► memory_keeper ──► remember / search / forget
              └──────────────┬─────────────┘
                             ▼
          reply to user ──► background: extract memories, refresh profile, cognition cycle

  Memory tiers
  ┌─────────────────────────┐   ┌──────────────────────────────────────────────────┐
  │ Tier 1  S3 (or folder)  │◄──│ Tier 2  Postgres                                 │
  │ every turn, immutable   │   │ memory rows · entities · full-text · pgvector    │
  │ source of truth         │   │ knowledge graph · tasks · notes · conversations  │
  └─────────────────────────┘   └──────────────────────────────────────────────────┘
```

---

## Prerequisites

- Python 3.11+
- Docker Desktop
- AWS credentials (`aws configure`) with Bedrock access enabled for a Claude model and **Titan Text Embeddings V2**
- Check model IDs in `.env.example` against your Bedrock console (`us.` prefix for US regions; `apac.` / `eu.` for others)

---

## Setup

```bash
cp .env.example .env            # Windows: copy .env.example .env  then edit .env
docker compose up -d            # Postgres + pgvector on port 5432

python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

alembic upgrade head            # creates tables, indexes, and the vector extension

pytest                          # runs against the database; tests clean up after themselves
uvicorn app.main:app --reload
```

Open **http://localhost:8000** to chat. API docs at **http://localhost:8000/docs**.

---

## Try it

```
1. "My sister is Riya. I like quiet cafes for deep work."
   → saves both facts in the background

2. New chat → "Where should I go to focus this weekend?"
   → recall by meaning

3. "Add a task to send the invoice on Friday at 5pm." → "What's on my list?"

4. "What did I tell you about Riya?" → "Forget that I like quiet cafes."

5. GET /memories?q=riya   → see what the router finds for any text
```

Raw conversations are in `data/archive/`. Memories are in the `memories` table:
```bash
docker compose exec db psql -U agent
```

---

## Observability Dashboard

Open **http://localhost:8000/dashboard** — every chat turn, model call, tool call, and memory search is recorded live:

- Latency percentiles · error rate · token and cost estimates
- Retrieval ranking: which memory channel found what, and how it was fused
- Tool failures · memory growth · index health
- Computer-use runs · raw database tables · live log tail · event-loop lag
- Alerts for throttling, failing tools, memories missing embeddings, open server without `API_KEY`

```bash
alembic upgrade head          # creates obs_traces / obs_spans tables
python -m scripts.obs_seed    # optional: 72 h of fake traffic to explore the UI
python -m scripts.obs_seed --clear
```

Keyboard shortcuts: `1`–`0` switch page · `r` refresh · `/` search · `esc` close panel. Deep links work: `#/traces/<trace-id>`.

> `TELEMETRY_STORE_CONTENT=false` keeps timings and sizes but never the message text.

---

## Memory Maintenance

```bash
python -m scripts.memory_admin health                        # can search see every memory it should?
python -m scripts.memory_admin reembed                       # fill missing / stale vectors (resumable)
python -m scripts.memory_admin reextract --dry-run           # preview a replay of the archive
python -m scripts.memory_admin reextract --since 2026-01-01
python -m scripts.memory_admin eval evals/cases.json         # score retrieval quality
```

Switching embedding models: change `EMBEDDING_MODEL_ID`, restart, run `reembed`. Until it finishes, memories with old vectors are still found by keyword, graph and time search — just not by meaning. Changing `EMBEDDING_DIM` also requires a new Alembic migration (the app refuses to start if they disagree).

---

## Optional Integrations

**Web search** — get a [Tavily](https://tavily.com) API key and set `TAVILY_API_KEY` in `.env`.

**Google Calendar** — create a Desktop OAuth client in Google Cloud Console, enable the Calendar API, save the JSON as `secrets/google_client.json`, then:
```bash
python -m scripts.google_auth
```

---

## Desktop Operator *(optional, off by default)*

An agent that sees your screen, plans, acts, and verifies — does multi-step jobs on this machine:

```bash
pip install -r requirements-desktop.txt
# .env: OPERATOR_ROOTS=C:\Projects\riki   (folders it may touch)
python -m scripts.desktop_operator "Prepare my development environment"
```

Approvals (installs, file edits, unknown commands) are asked in the terminal. Stop with `Ctrl+C` or move the mouse to a screen corner. To drive it from the API: set `OPERATOR_ENABLED=true` and `API_KEY`, then use `POST /operator/runs`.

---

## AWS Deployment

Set `STORAGE_BACKEND=s3` and `S3_BUCKET`, point `DATABASE_URL` at RDS PostgreSQL (`alembic upgrade head` handles the vector extension), run the app in a container on App Runner or Fargate with an IAM role that allows Bedrock and the bucket. Full details in `docs/ARCHITECTURE.md` § 5.

---

## Project Layout

```
app/
  main.py  chat_service.py  config.py  db.py  llm.py  storage.py  timeutil.py
  api/          HTTP endpoints
  agents/       prompts and the LangGraph multi-agent team
  cognition/    background thinking cycle
  memory/       store · retrieval · recall · rerank · extraction · profile · maintenance · eval
  tools/        one file per tool group; registry in __init__.py
  desktop/      Desktop Operator: agent loop, policy, executor, screen/input, run registry
  obs/          observability traces, spans, pricing
  models.py     all SQLAlchemy tables
  schemas.py    Pydantic validation models
alembic/        database migrations
scripts/        google_auth · memory_admin · obs_seed · desktop_operator
evals/          example retrieval test set
docs/           ARCHITECTURE · COGNITION · MEMORY_SYSTEM · SECURITY · AWS_MIGRATION
tests/          validation · agent wiring · memory search · extraction · chat round trip
```

---

## Author

Built by **[16bitOni](https://github.com/16bitOni)**
