"""Background scheduler: runs cognition cycles every 6-12 hours.

Replaces the old reflection scheduler. Instead of just regenerating a static summary,
the cognition cycle actively thinks — comparing what's new, noticing patterns,
and deciding whether to reach out.

On startup: runs immediately if no cycle exists or the last one is stale.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.cognition.cycle import run_cognition_cycle, _get_last_cycle
from app.config import settings
from app.core.db import get_or_create_user

log = logging.getLogger(__name__)

instance: "CognitionScheduler | None" = None


class CognitionScheduler:
    def __init__(self, notify_fn=None):
        global instance
        instance = self
        self.notify_fn = notify_fn
        self._stop = asyncio.Event()
        self._interval = settings.cognition_interval_hours * 3600

    async def _maybe_run(self) -> None:
        try:
            user = await get_or_create_user()
            last = await _get_last_cycle(user.id)
            stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._interval)
            if last and last.created_at > stale_cutoff:
                log.debug("Last cognition cycle recent enough, skipping")
                return
            await run_cognition_cycle(user, self.notify_fn)
        except Exception:
            log.exception("Cognition scheduler check failed")

    async def run(self) -> None:
        log.info("Cognition scheduler started (interval=%dh)", settings.cognition_interval_hours)
        await self._maybe_run()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                break
            except asyncio.TimeoutError:
                pass
            await self._maybe_run()

    def stop(self) -> None:
        self._stop.set()
