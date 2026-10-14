# Personal Agent

A personal multi-agent assistant with tiered long-term memory. Read `docs/ARCHITECTURE.md` for the design.

Stack: FastAPI, LangGraph, Postgres + pgvector, SQLAlchemy 2.0 (async) + Alembic, Pydantic, AWS Bedrock.

## Before you start

- Python 3.11 or newer
- Docker Desktop
- AWS credentials on this machine (`aws configure`) for an account where Bedrock model access is enabled for a Claude model and **Titan Text Embeddings V2**
- Check the model IDs in `.env.example` against your Bedrock console. The `us.` prefix is for US regions; use `apac.` or `eu.` to match your region

## Setup

```bash
cp .env.example .env            # Windows: copy .env.example .env   then edit .env
docker compose up -d            # Postgres with pgvector on port 5432

python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

alembic upgrade head           # creates the tables, indexes and the vector extension

pytest                          # runs against the database above; tests clean up after themselves
uvicorn app.main:app --reload
```

Open http://localhost:8000 and talk to it. API docs are at http://localhost:8000/docs.

## Try these

1. "My sister is Riya. I like quiet cafes for deep work." (it saves both in the background)
2. New chat, then: "Where should I go to focus this weekend?" (recall by meaning)
3. "Add a task to send the invoice on Friday at 5pm." then "What's on my list?"
4. "What did I tell you about Riya?" and "Forget that I like quiet cafes."
5. See what the router finds for any text: `GET /memories?q=riya`

Raw conversations appear under `data/archive/`. Memories are in the `memories` table (use any Postgres client, e.g. `docker compose exec db psql -U agent`).

## Memory maintenance

```bash
python -m scripts.memory_admin health                      # can search see every memory it should?
python -m scripts.memory_admin reembed                     # fill in missing / old-model vectors (resumable)
python -m scripts.memory_admin reextract --dry-run         # preview a replay of the archive through the extractor
python -m scripts.memory_admin reextract --since 2026-01-01
python -m scripts.memory_admin eval evals/cases.json       # score retrieval (copy evals/cases.example.json)
```

Switching embedding model: change `EMBEDDING_MODEL_ID`, restart, run `reembed`. Until it finishes, memories
with old vectors are still found by keyword, graph and time search, just not by meaning. Changing
`EMBEDDING_DIM` also needs an Alembic migration (the app refuses to start if they disagree).
The same checks are at `GET /admin/memory-health` and `POST /admin/reembed`.

## Desktop Operator (optional, off by default)

An agent that sees your screen, plans, acts and checks the result, to do multi-step jobs on this computer:

```bash
pip install -r requirements-desktop.txt
# .env:  OPERATOR_ROOTS=C:\Projects\memory-agent        (the only folders it may touch)
python -m scripts.desktop_operator "Prepare my development environment for the memory-agent project"
```

Approvals (installs, file edits, unknown commands) are asked in the terminal. Stop it with Ctrl+C or by moving
the mouse into a screen corner. To drive it from a UI instead, set `OPERATOR_ENABLED=true` **and** `API_KEY`, then
use `POST /operator/runs`, `GET /operator/runs/{id}`, `POST /operator/runs/{id}/approval`, `POST /operator/runs/{id}/cancel`.
Every run is logged to the archive under `operator/`. Design and limits: `docs/ARCHITECTURE.md`, section 8.

## Dashboard (observability)

Open **http://localhost:8000/dashboard**. Every chat turn, model call, tool call and memory search is recorded
and shown live: latency percentiles, error rate, token and cost estimates, the retrieval ranking behind each answer
(which channel found which memory, and how it was fused), tool failures, memory growth and index health, computer-use runs,
the raw database tables, the live log tail, and event-loop lag. Alerts flag things like throttling, failing tools,
memories missing embeddings, or an open server with no `API_KEY`.

```bash
alembic upgrade head                    # creates the obs_traces / obs_spans tables
python -m scripts.obs_seed              # optional: 72 h of clearly-marked fake traffic to look around
python -m scripts.obs_seed --clear      # remove exactly that fake data again
```

Keys: `1`..`0` switch page, `r` refresh, `/` search, `esc` close the side panel. Deep links work: `#/traces/<trace id>`.
The dashboard shows private data (your messages and memories), so set `API_KEY` if the server is reachable by anyone else.
`TELEMETRY_STORE_CONTENT=false` keeps timings and sizes but never the text. Cost figures are estimates from
token counts (edit `app/obs/pricing.py`). Log lines are scrubbed of bot tokens and API keys before they are kept.

## Optional tools

- **Web search**: get a Tavily API key and set `TAVILY_API_KEY` in `.env`.
- **Google Calendar**: create a Desktop OAuth client in Google Cloud Console, enable the Calendar API, save the JSON as `secrets/google_client.json`, then run `python -m scripts.google_auth` once.

## Layout

```
app/
  main.py  chat_service.py  config.py  db.py  llm.py  storage.py  timeutil.py
  api/routes.py          HTTP endpoints
  agents/                prompts and the LangGraph team
  memory/                store (write), retrieval + recall + rerank (read), extraction, profile, maintenance, evaluation
  tools/                 one file per tool group; registry in __init__.py
  desktop/               Desktop Operator: agent loop, policy, executor, screen/input, run registry
app/models.py            all tables (SQLAlchemy)
app/schemas.py           validation models (Pydantic)
alembic/                 database migrations
scripts/                 google_auth, memory_admin
evals/                   example retrieval test set
tests/                   validation, agent wiring, memory search, extraction, chat round trip
```

## Moving to AWS

Set `STORAGE_BACKEND=s3` and `S3_BUCKET`, point `DATABASE_URL` at RDS PostgreSQL (`alembic upgrade head` creates the vector extension; the RDS master user is allowed to), run the app in a container on App Runner or Fargate with an IAM role that allows Bedrock and the bucket. Details in `docs/ARCHITECTURE.md`, section 5.
"# Riki" 
