"""Everything that talks to Bedrock: chat models and embeddings."""

import asyncio
import json
from functools import lru_cache

import boto3
from botocore.config import Config as BotoConfig
from langchain_aws import ChatBedrockConverse

from app.config import settings
from app.obs import trace as obs
from app.obs.callbacks import handler as obs_handler

_BEDROCK_CONFIG = BotoConfig(
    read_timeout=120,
    connect_timeout=10,
    retries={"max_attempts": 3, "mode": "adaptive"},
)

# Limit concurrent Bedrock calls to avoid throttling cascades.
_bedrock_semaphore = asyncio.Semaphore(4)


def chat_model(fast: bool = False, purpose: str = "") -> ChatBedrockConverse:
    """`fast=True` is the cheap model used for routing and memory extraction.
    `purpose` labels the calls in the dashboard ("recall_plan", "extraction", ...)."""
    # Background tasks (extraction, recall planning, cognition) use temperature=0 for
    # reliable structured output. The agent conversation uses a higher temperature so
    # responses feel less mechanical.
    is_structured = purpose in {
        "extraction", "recall_plan", "profile", "reflection", "session_extraction",
        "self_learning", "cognition_think", "cognition_act", "nudge_plan",
    }
    temperature = 0 if (fast or is_structured) else settings.agent_temperature
    return ChatBedrockConverse(
        model=settings.fast_model_id if fast else settings.main_model_id,
        region_name=settings.aws_region,
        temperature=temperature,
        max_tokens=2048,
        config=_BEDROCK_CONFIG,
        callbacks=[obs_handler],
        tags=[f"purpose:{purpose}"] if purpose else [],
    )


@lru_cache
def _bedrock_runtime():
    return boto3.client("bedrock-runtime", region_name=settings.aws_region, config=_BEDROCK_CONFIG)


def _embed_sync(text: str) -> tuple[list[float], int]:
    body = json.dumps(
        {"inputText": text, "dimensions": settings.embedding_dim, "normalize": True}
    )
    response = _bedrock_runtime().invoke_model(
        modelId=settings.embedding_model_id, body=body
    )
    data = json.loads(response["body"].read())
    return data["embedding"], int(data.get("inputTextTokenCount") or 0)


async def embed(text: str) -> list[float]:
    async with _bedrock_semaphore:
        with obs.span("embed", "embed", chars=len(text)) as s:
            s.model = settings.embedding_model_id
            vector, tokens = await asyncio.to_thread(_embed_sync, text)
            s.input_tokens = tokens
            return vector


def text_of(message) -> str:
    """Bedrock sometimes returns message content as a list of blocks; flatten it to plain text."""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
    )
