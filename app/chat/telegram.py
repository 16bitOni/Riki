"""Telegram front end: talk to the agent from your phone instead of the web page.

It uses long polling (the bot asks Telegram for new messages), so no public URL or webhook is needed.
Only chats listed in TELEGRAM_ALLOWED_CHAT_IDS are answered; everyone else is ignored.
Started from the FastAPI lifespan when TELEGRAM_BOT_TOKEN is set.
"""

import asyncio
import base64
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from sqlalchemy import func, select

from app.chat.service import run_turn
from app.config import settings
from app.core.db import get_or_create_user, session_scope
from app.core.models import Conversation, Message, ProactiveNudge
from app.core.schemas import ImageInput
from app.chat.session import close_session

log = logging.getLogger(__name__)

instance: "TelegramBot | None" = None  # the running bot, so the dashboard can show its status


def _is_context_overflow(exc: BaseException) -> bool:
    """True when Bedrock rejects the request because the input is too long."""
    msg = str(exc).lower()
    return any(phrase in msg for phrase in (
        "input is too long",
        "too many input tokens",
        "maximum context length",
        "prompt is too long",
        "validationexception",
    ))


def _is_checkpoint_corruption(exc: BaseException) -> bool:
    """True when Bedrock rejects the message history because a tool call has no matching result.

    This happens when a turn crashes after the LLM issues a tool call but before the
    tool result is written back. The checkpoint is left with an unmatched tool-use ID
    and every subsequent message in that conversation will fail the same way.
    """
    return "toolResult" in str(exc) and "Expected" in str(exc)

TELEGRAM_LIMIT = 4096  # max characters per Telegram message
POLL_SECONDS = 30


def _md_to_html(text: str) -> str:
    """Convert the markdown the LLM produces into Telegram HTML.

    Processes code blocks separately (so their content is never mangled),
    then HTML-escapes everything else before applying inline formatting.
    """
    # Split on fenced code blocks first so we never regex-process code content.
    segments = re.split(r"(```(?:[a-z]*\n)?.*?```)", text, flags=re.DOTALL)
    out = []
    for i, seg in enumerate(segments):
        if i % 2 == 1:  # fenced code block
            inner = re.sub(r"^```[a-z]*\n?", "", seg).rstrip("`").strip()
            out.append(f"<pre>{html.escape(inner)}</pre>")
            continue

        # HTML-escape first so user content can't inject tags.
        seg = html.escape(seg)

        # ## Header → bold (must come before bold rule)
        seg = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", seg, flags=re.MULTILINE)

        # **bold** or __bold__
        seg = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", seg)
        seg = re.sub(r"__(.+?)__", r"<b>\1</b>", seg)

        # *italic* or _italic_ (single, not double)
        seg = re.sub(r"\*([^*\n]+)\*", r"<i>\1</i>", seg)
        seg = re.sub(r"_([^_\n]+)_", r"<i>\1</i>", seg)

        # `inline code`
        seg = re.sub(r"`([^`]+)`", r"<code>\1</code>", seg)

        out.append(seg)

    return "".join(out)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Cut a long reply into Telegram-sized pieces, preferring paragraph / line / word boundaries."""
    parts = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


NUDGE_RESPONSE_WINDOW_HOURS = 4  # a user message within this window counts as a nudge response


async def _mark_nudge_responded(user_id: UUID) -> None:
    """If the user just spoke within 4h of an unresponded nudge, mark it as responded."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NUDGE_RESPONSE_WINDOW_HOURS)
    try:
        async with session_scope() as session:
            nudge = await session.scalar(
                select(ProactiveNudge).where(
                    ProactiveNudge.user_id == user_id,
                    ProactiveNudge.sent_at >= cutoff,
                    ProactiveNudge.responded_at.is_(None),
                ).order_by(ProactiveNudge.sent_at.desc()).limit(1)
            )
            if nudge:
                nudge.responded_at = datetime.now(timezone.utc)
                log.info("Nudge response recorded: topic=%r", nudge.topic)
    except Exception:
        log.exception("Failed to mark nudge as responded")


class TelegramBot:
    def __init__(self, token: str, allowed_chat_ids: set[int]):
        self.api = f"https://api.telegram.org/bot{token}"
        self.allowed = allowed_chat_ids
        self.conversations: dict[int, UUID | None] = {}  # chat id -> conversation being continued
        self.client = httpx.AsyncClient(timeout=POLL_SECONDS + 15)
        self.locks: dict[int, asyncio.Lock] = {}  # one turn at a time per chat, but chats do not block the poll loop
        self.tasks: set[asyncio.Task] = set()
        self.started_at = datetime.now(timezone.utc)
        self.last_poll_at: datetime | None = None
        self.last_error: str | None = None
        self.counts = {"messages": 0, "polls": 0, "poll_errors": 0, "unauthorised": 0}

    def stats(self) -> dict:
        return {
            "running": True, "started_at": self.started_at, "last_poll_at": self.last_poll_at,
            "last_error": self.last_error, "allowed_chats": len(self.allowed), "in_progress": len(self.tasks), **self.counts,
        }

    async def _call(self, method: str, **params):
        response = await self.client.post(f"{self.api}/{method}", json=params)
        response.raise_for_status()
        return response.json()["result"]

    async def _send(self, chat_id: int, text: str) -> int | None:
        """Convert markdown to HTML, split if needed, send. Returns the last message_id."""
        formatted = _md_to_html(text)
        last_id = None
        for part in split_message(formatted):
            try:
                result = await self._call("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML")
            except Exception:
                # HTML parse failed (e.g. unclosed tag from complex markdown) — fall back to plain text
                result = await self._call("sendMessage", chat_id=chat_id, text=part)
            last_id = result.get("message_id")
        return last_id

    async def notify(self, text: str) -> int | None:
        """Send a message to the owner. Returns the Telegram message_id."""
        if not self.allowed:
            raise RuntimeError("TELEGRAM_ALLOWED_CHAT_IDS is empty")
        return await self._send(min(self.allowed), text)

    async def _send_photos(self, chat_id: int, urls: list[str]) -> None:
        """Send pictures by link; if Telegram can't fetch one (some sites block it), download and upload it instead."""
        for url in urls:
            try:
                await self._call("sendPhoto", chat_id=chat_id, photo=url)
                continue
            except Exception:
                pass
            try:
                image = await self.client.get(url, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
                image.raise_for_status()
                if not image.headers.get("content-type", "").startswith("image/"):
                    continue
                sent = await self.client.post(
                    f"{self.api}/sendPhoto", data={"chat_id": chat_id}, files={"photo": ("photo", image.content)}
                )
                sent.raise_for_status()
            except Exception:
                log.warning("Could not send picture %s", url)

    async def _typing_until_cancelled(self, chat_id: int) -> None:
        """Telegram's 'typing...' status lasts ~5 seconds, so renew it while the agent thinks."""
        while True:
            try:
                await self._call("sendChatAction", chat_id=chat_id, action="typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    async def _handle_reaction(self, update: dict) -> None:
        """Emoji reaction on one of Riki's messages. Positive reactions resolve the linked nudge."""
        POSITIVE = {"👍", "❤️", "🔥", "✅", "🎉", "😍", "👏", "💯", "🆗"}
        new_reactions = [r.get("emoji", "") for r in update.get("new_reaction", []) if r.get("type") == "emoji"]
        if not any(e in POSITIVE for e in new_reactions):
            return  # removed reaction or non-positive — ignore

        message_id = update.get("message_id")
        if not message_id:
            return

        from datetime import datetime, timezone
        from sqlalchemy import select, update as sa_update
        from app.core.db import get_or_create_user, session_scope
        from app.core.models import ProactiveNudge

        user = await get_or_create_user()
        async with session_scope() as session:
            nudge = await session.scalar(
                select(ProactiveNudge).where(
                    ProactiveNudge.user_id == user.id,
                    ProactiveNudge.telegram_message_id == message_id,
                    ProactiveNudge.resolved_at.is_(None),
                )
            )
            if nudge:
                nudge.resolved_at = datetime.now(timezone.utc)
                nudge.responded_at = nudge.responded_at or datetime.now(timezone.utc)
                log.info("Nudge resolved via reaction %s topic=%r", new_reactions, nudge.topic)
                if nudge.attention_thread_id:
                    from app.cognition import add_evidence
                    await add_evidence(
                        nudge.attention_thread_id,
                        nudge.id,
                        f"User reacted positively ({', '.join(new_reactions)}) to reach-out about '{nudge.topic}'",
                    )

    async def _locked_handle(self, message: dict) -> None:
        async with self.locks.setdefault(message["chat"]["id"], asyncio.Lock()):
            try:
                await self._handle(message)
            except Exception:
                log.exception("Telegram update failed")

    async def _latest_conversation(self, user) -> UUID | None:
        async with session_scope() as session:
            return await session.scalar(
                select(Conversation.id)
                .where(Conversation.user_id == user.id)
                .order_by(Conversation.created_at.desc())
                .limit(1)
            )

    async def _hours_since_last_message(self, conversation_id: UUID) -> float | None:
        """How many hours since the last message in this conversation. None if no messages."""
        async with session_scope() as session:
            last_at = await session.scalar(
                select(func.max(Message.created_at)).where(Message.conversation_id == conversation_id)
            )
        if not last_at:
            return None
        return (datetime.now(timezone.utc) - last_at).total_seconds() / 3600

    async def _download_photo(self, file_id: str) -> ImageInput | None:
        """Download a photo from Telegram and return it as a base64-encoded ImageInput."""
        try:
            file_info = await self._call("getFile", file_id=file_id)
            file_path = file_info["file_path"]
            url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
            response = await self.client.get(url)
            response.raise_for_status()
            data = base64.b64encode(response.content).decode()
            ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "jpg"
            media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
            return ImageInput(base64=data, media_type=media_type)
        except Exception:
            log.exception("Failed to download Telegram photo %s", file_id)
            return None

    async def _handle(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or message.get("caption") or "").strip()
        photos = message.get("photo")

        if chat_id not in self.allowed:
            if text or photos:
                await self._send(chat_id, f"Not authorised. If this is you, add {chat_id} to TELEGRAM_ALLOWED_CHAT_IDS.")
            log.warning("Telegram message from unauthorised chat %s ignored", chat_id)
            self.counts["unauthorised"] += 1
            return
        if not text and not photos:
            await self._send(chat_id, "I can read text and photos. Send me a message!")
            return

        self.counts["messages"] += 1
        user = await get_or_create_user()
        if text in ("/start", "/help"):
            await self._send(chat_id, "Hi! Send me a message and I'll answer. /new starts a fresh conversation. You can also send photos!")
            return
        if text == "/new":
            old_conversation_id = self.conversations.get(chat_id)
            self.conversations[chat_id] = None
            await self._send(chat_id, "Started a new conversation.")
            if old_conversation_id:
                t = asyncio.create_task(close_session(user, old_conversation_id))
                self.tasks.add(t)
                t.add_done_callback(self.tasks.discard)
            return

        images: list[ImageInput] = []
        if photos:
            best = max(photos, key=lambda p: p.get("file_size", 0))
            img = await self._download_photo(best["file_id"])
            if img:
                images.append(img)

        if not text:
            text = "What's in this image?"

        if chat_id not in self.conversations:  # first message since the server started: pick up where we left off
            self.conversations[chat_id] = await self._latest_conversation(user)

        conv_id = self.conversations.get(chat_id)
        if conv_id:
            gap = await self._hours_since_last_message(conv_id)
            if gap is not None and gap >= settings.telegram_new_convo_gap_hours:
                log.info("Auto-starting new conversation (%.1fh gap since last message)", gap)
                self.conversations[chat_id] = None
                t = asyncio.create_task(close_session(user, conv_id))
                self.tasks.add(t)
                t.add_done_callback(self.tasks.discard)

        typing = asyncio.create_task(self._typing_until_cancelled(chat_id))
        try:
            turn = await run_turn(user, text[:4000], self.conversations[chat_id], source="telegram", images=images or None)
        except LookupError:
            self.conversations[chat_id] = None
            await self._send(chat_id, "I lost track of that conversation. Please send your message again.")
            return
        except Exception as exc:
            if _is_checkpoint_corruption(exc):
                old = self.conversations.get(chat_id)
                self.conversations[chat_id] = None
                log.warning("Checkpoint corruption detected for chat %s (conv %s) — reset to new conversation", chat_id, old)
                await self._send(chat_id, "The last conversation got into a broken state (a tool call was interrupted mid-flight). I've reset it — just resend your message.")
            elif _is_context_overflow(exc):
                log.warning("Context overflow for chat %s — resetting conversation", chat_id)
                old = self.conversations.get(chat_id)
                self.conversations[chat_id] = None
                if old:
                    t = asyncio.create_task(close_session(user, old))
                    self.tasks.add(t)
                    t.add_done_callback(self.tasks.discard)
                await self._send(chat_id, "This conversation got too long for me to hold in memory. I've started a fresh one — your memories are still there, just send your message again.")
            else:
                log.exception("Telegram turn failed")
                await self._send(chat_id, "Something went wrong. Please try again.")
            return
        finally:
            typing.cancel()

        self.conversations[chat_id] = turn.conversation_id
        await self._send(chat_id, turn.reply)
        await self._send_photos(chat_id, turn.images)

        t = asyncio.create_task(_mark_nudge_responded(user.id))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def run(self) -> None:
        offset = 0
        log.info("Telegram bot started (%d allowed chat(s))", len(self.allowed))
        if not self.allowed:
            log.warning("TELEGRAM_ALLOWED_CHAT_IDS is empty: message the bot to get your chat id, then add it to .env")
        while True:
            try:
                updates = await self._call(
                    "getUpdates", offset=offset, timeout=POLL_SECONDS,
                    allowed_updates=["message", "message_reaction"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("Telegram polling failed (%s); retrying in 5s", type(error).__name__)
                self.counts["poll_errors"] += 1
                self.last_error = f"{type(error).__name__}: {error}"[:200]
                await asyncio.sleep(5)
                continue
            self.counts["polls"] += 1
            self.last_poll_at = datetime.now(timezone.utc)
            for update in updates:
                offset = update["update_id"] + 1
                if "message_reaction" in update:
                    try:
                        await self._handle_reaction(update["message_reaction"])
                    except Exception:
                        log.exception("Telegram reaction failed")
                elif "message" in update:
                    task = asyncio.create_task(self._locked_handle(update["message"]))
                    self.tasks.add(task)  # keep a reference so it is not garbage collected mid-run
                    task.add_done_callback(self.tasks.discard)

    async def close(self) -> None:
        await self.client.aclose()


def start_bot() -> tuple[asyncio.Task, TelegramBot] | None:
    """Start the bot in the background if a token is configured."""
    if not settings.telegram_bot_token or not settings.telegram_enabled:
        return None
    global instance
    bot = TelegramBot(settings.telegram_bot_token, settings.telegram_chat_ids)
    instance = bot
    return asyncio.create_task(bot.run()), bot
