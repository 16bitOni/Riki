"""All settings in one place. Values come from environment variables or the .env file."""

from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agent:agent@localhost:5432/agent"

    # AWS Bedrock
    aws_region: str = "us-east-1"
    main_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    fast_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    embedding_dim: Literal[256, 512, 1024] = 1024  # Titan v2 sizes. Must match the vector column (checked at startup).

    # Raw conversation archive
    storage_backend: Literal["local", "s3"] = "local"
    local_archive_dir: str = "data/archive"
    s3_bucket: str = ""

    # The one user of this assistant
    user_name: str = "Subhadip"
    user_timezone: str = "Asia/Kolkata"

    # Optional tools
    tavily_api_key: str = ""
    google_client_secret_path: str = "secrets/google_client.json"
    google_token_path: str = "secrets/google_token.json"

    # Telegram front end (off unless a token is set). Only the listed chat ids get answers.
    telegram_bot_token: str = ""
    telegram_enabled: bool = True  # TELEGRAM_ENABLED=false runs this instance without the bot (a second dev server must not poll your real bot)
    telegram_allowed_chat_ids: str = ""  # comma separated

    # Observability: every chat turn, model call, tool call and retrieval is recorded for the /dashboard.
    telemetry_enabled: bool = True
    telemetry_retention_days: int = Field(30, ge=1, le=3650)  # older traces are deleted automatically
    telemetry_store_content: bool = True  # False: keep only sizes and timings, never message or memory text

    # Agent conversation temperature. 0 = deterministic (assistant-brained).
    # 0.5-0.7 = more natural, less mechanical. Background tasks always use 0.
    agent_temperature: float = Field(0.5, ge=0, le=1)

    # Optional API protection
    api_key: str = ""

    # Session context
    session_timeout_hours: int = Field(24, ge=1, le=168)  # inactivity before a session is considered ended
    telegram_new_convo_gap_hours: int = Field(20, ge=1, le=168)  # gap before Telegram starts a new conversation (was hardcoded 6h)
    session_max_messages: int = Field(100, ge=10, le=500)  # trim older messages beyond this to cap token usage
    session_guide_limit: int = Field(5, ge=1, le=20)  # how many recent session guides to inject

    # Memory: what the LLM sees
    recall_limit: int = Field(8, ge=1, le=30)  # memories handed to the LLM per request
    history_messages: int = Field(20, ge=0, le=100)  # recent chat messages sent to the LLM (fallback when no checkpoint)

    # Retrieval tuning. Measure with `python -m scripts.memory_admin eval` before changing.
    min_similarity: float = Field(0.3, ge=0, le=1)  # cosine similarity below this is not a semantic match
    graph_max_hops: int = Field(3, ge=1, le=4)  # how far to walk along relations ("sister's husband's company" = 3)
    graph_fanout_limit: int = Field(200, ge=10, le=2000)  # most entities one graph walk may visit
    weight_keyword: float = Field(1.0, ge=0)  # how much each channel counts in rank fusion
    weight_semantic: float = Field(1.0, ge=0)
    weight_graph: float = Field(0.7, ge=0)
    weight_time: float = Field(0.5, ge=0)
    importance_weight: float = Field(0.05, ge=0, le=0.3)  # small boost per importance point above 3
    rerank: Literal["off", "llm"] = "off"  # "llm" re-orders the top candidates with the fast model
    rerank_candidates: int = Field(20, ge=5, le=50)

    # Duplicate detection when saving
    dedup_trigram_threshold: float = Field(0.85, ge=0.5, le=1)  # spelling-level similarity
    dedup_vector_threshold: float = Field(0.93, ge=0.5, le=1)  # meaning-level similarity (embedded memories only)

    # Maintenance jobs
    reembed_concurrency: int = Field(5, ge=1, le=20)

    # Cognition: the agent thinks between conversations
    cognition_interval_hours: int = Field(4, ge=4, le=24)  # how often the agent runs a thinking cycle
    cognition_quiet_hours_start: int = Field(23, ge=0, le=23)  # don't reach out after this hour (local)
    cognition_quiet_hours_end: int = Field(7, ge=0, le=23)  # don't reach out before this hour (local)
    cognition_min_new_memories: int = Field(0, ge=0, le=20)  # skip cycle if fewer new memories (0 = always run)

    # Cognition v2: signals and attention threads
    signal_entity_recent_days: int = Field(14, ge=1, le=60)
    signal_entity_baseline_days: int = Field(90, ge=14, le=365)
    signal_activity_recent_days: int = Field(7, ge=1, le=30)
    signal_activity_baseline_days: int = Field(30, ge=7, le=180)
    signal_mood_days: int = Field(7, ge=1, le=30)
    thread_auto_abandon_cycles: int = Field(8, ge=3, le=20)
    thread_max_active: int = Field(10, ge=3, le=25)

    # Proactive nudges
    nudge_enabled: bool = True
    nudge_min_interval_hours: int = Field(48, ge=6, le=168)  # minimum gap between nudges
    nudge_quiet_after_convo_hours: int = Field(12, ge=1, le=48)  # don't nudge if user was active recently

    @field_validator("database_url")
    @classmethod
    def use_async_driver(cls, value: str) -> str:
        """Accept a plain postgresql:// URL and switch it to the async driver."""
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must start with postgresql:// or postgresql+asyncpg://")
        return value

    @field_validator("user_timezone")
    @classmethod
    def real_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError(f"'{value}' is not a valid IANA time zone, e.g. Asia/Kolkata")
        return value

    @property
    def checkpoint_database_url(self) -> str:
        """psycopg-compatible URL for the LangGraph checkpointer (it uses psycopg, not asyncpg)."""
        return self.database_url.replace("+asyncpg", "")

    @property
    def telegram_chat_ids(self) -> set[int]:
        return {int(p) for p in self.telegram_allowed_chat_ids.split(",") if p.strip()}

    @field_validator("s3_bucket")
    @classmethod
    def strip_bucket(cls, value: str) -> str:
        return value.strip()


settings = Settings()

if settings.storage_backend == "s3" and not settings.s3_bucket:
    raise ValueError("STORAGE_BACKEND=s3 needs S3_BUCKET to be set")
