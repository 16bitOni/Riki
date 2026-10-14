"""Background scheduler: sends morning briefing and checks for proactive nudges.

Wakes at natural times in the user's timezone:
- 8:30am: morning briefing
- 9:30am, 7:30pm: nudge check (also sends briefing at 9:30 if missed at 8:30)
"""

import asyncio
import logging
from datetime import datetime, time, timedelta

from app.features.briefing import send_morning_briefing
from app.config import settings
from app.core.db import get_or_create_user
from app.features.nudge import maybe_send_nudge
from app.core.timeutil import now_in

log = logging.getLogger(__name__)

BRIEFING_WINDOW = time(8, 30)
NUDGE_WINDOWS = [time(9, 30), time(19, 30)]
ALL_WINDOWS = [BRIEFING_WINDOW] + NUDGE_WINDOWS
FALLBACK_INTERVAL = 4 * 3600

instance: "NudgeScheduler | None" = None


def _seconds_until_next_window(tz: str) -> tuple[int, time]:
    """Seconds until the next window and which window it is."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(tz))
    candidates = []
    for w in ALL_WINDOWS:
        dt = datetime.combine(now.date(), w, tzinfo=now.tzinfo)
        if dt > now:
            candidates.append((dt, w))
        dt_tomorrow = datetime.combine(now.date() + timedelta(days=1), w, tzinfo=now.tzinfo)
        candidates.append((dt_tomorrow, w))
    candidates.sort(key=lambda x: x[0])
    if not candidates:
        return FALLBACK_INTERVAL, NUDGE_WINDOWS[0]
    dt, window = candidates[0]
    gap = max(int((dt - now).total_seconds()), 60)
    return gap, window


class NudgeScheduler:
    def __init__(self, notify_fn):
        global instance
        instance = self
        self.notify_fn = notify_fn
        self._stop = asyncio.Event()

    async def _do_briefing(self) -> None:
        try:
            user = await get_or_create_user()
            await send_morning_briefing(user, self.notify_fn)
        except Exception:
            log.exception("Morning briefing failed")

    async def _do_nudge(self) -> None:
        try:
            user = await get_or_create_user()
            await maybe_send_nudge(user, self.notify_fn)
        except Exception:
            log.exception("Nudge scheduler check failed")

    async def _handle_window(self, window: time) -> None:
        if window == BRIEFING_WINDOW:
            await self._do_briefing()
        else:
            # At nudge windows, also try briefing in case 8:30 was missed
            await self._do_briefing()
            await self._do_nudge()

    async def run(self) -> None:
        log.info("Nudge scheduler started (briefing=%s, nudge windows=%s)",
                 BRIEFING_WINDOW, NUDGE_WINDOWS)
        # On startup, try briefing + nudge immediately
        await self._do_briefing()
        await self._do_nudge()
        while not self._stop.is_set():
            tz = settings.user_timezone
            wait, window = _seconds_until_next_window(tz)
            log.debug("Scheduler sleeping %ds until %s", wait, window)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass
            await self._handle_window(window)

    def stop(self) -> None:
        self._stop.set()
