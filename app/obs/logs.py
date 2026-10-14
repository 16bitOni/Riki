"""An in-memory ring buffer of recent log lines, for the dashboard's live log view."""

import logging
import re
import threading
import traceback
from collections import deque
from datetime import datetime, timezone

CAPACITY = 4000
# Anything that looks like a secret is masked before it is kept: Telegram bot tokens inside URLs, bearer/API keys.
SECRETS = [
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot***"),
    (re.compile(r"(?i)(x-api-key|authorization|api[_-]?key|token|secret)(['\"]?\s*[:=]\s*['\"]?(?:bearer\s+)?)[^\s'\",}]{6,}"), r"\1\2***"),
]


def redact(text: str) -> str:
    for pattern, replacement in SECRETS:
        text = pattern.sub(replacement, text)
    return text


class RingLogHandler(logging.Handler):
    def __init__(self, capacity: int = CAPACITY):
        super().__init__(level=logging.INFO)
        self.buffer: deque[dict] = deque(maxlen=capacity)
        self.counter = 0
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": redact(record.getMessage()[:2000]),
                "exc": redact("".join(traceback.format_exception(*record.exc_info))[-3000:]) if record.exc_info else "",
            }
            with self._guard:
                self.counter += 1
                entry["id"] = self.counter
                self.buffer.append(entry)
        except Exception:
            pass  # a logging handler must never raise

    def query(self, level: str = "INFO", q: str = "", after_id: int = 0, limit: int = 300) -> list[dict]:
        floor = logging.getLevelName(level.upper())
        floor = floor if isinstance(floor, int) else logging.INFO
        needle = q.lower()
        with self._guard:
            rows = list(self.buffer)
        out = [
            r for r in rows
            if r["id"] > after_id and logging.getLevelName(r["level"]) >= floor
            and (not needle or needle in r["message"].lower() or needle in r["logger"].lower() or needle in r["exc"].lower())
        ]
        return out[-limit:]

    def counts(self) -> dict:
        with self._guard:
            rows = list(self.buffer)
        result = {"WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        for r in rows:
            if r["level"] in result:
                result[r["level"]] += 1
        return result


handler = RingLogHandler()


def install() -> None:
    root = logging.getLogger()
    if handler not in root.handlers:
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
