"""Test doubles that need no AWS account."""

import hashlib
import math
import re

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

DIM = 1024


def fake_embed_sync(text: str) -> list[float]:
    """Bag-of-words hashing: texts sharing words get similar vectors. Good enough for tests."""
    vector = [0.0] * DIM
    for word in re.findall(r"[a-z]+", text.lower()):
        bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM
        vector[bucket] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


async def fake_embed(text: str) -> list[float]:
    return fake_embed_sync(text)


class ScriptedChatModel(BaseChatModel):
    """Replays a fixed list of AI messages, one per model call, in order."""

    script: list[AIMessage]
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.script[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])
