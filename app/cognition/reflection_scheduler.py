"""Background scheduler: runs refresh_reflection_if_stale every 12 hours.

On startup: runs immediately if no reflection exists or the last one is >12h old.
Otherwise waits until the next interval.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.core.db import get_or_create_user
from app.cognition.reflection import get_latest_reflection, refresh_reflection_if_stale

log = logging.getLogger(__name__)

CHECK_INTERVAL = 12 * 3600  # seconds between staleness checks
instance: "ReflectionScheduler | None" = None


class ReflectionScheduler:
    def __init__(self):
        global instance
        instance = self
        self._stop = asyncio.Event()

    async def _maybe_refresh(self) -> None:
        try:
            user = await get_or_create_user()
            latest = await get_latest_reflection(user.id)
            stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=CHECK_INTERVAL)
            if latest and latest.created_at > stale_cutoff:
                log.debug("Reflection recent enough, skipping until next interval")
                return
            await refresh_reflection_if_stale(user)
        except Exception:
            log.exception("Reflection scheduler check failed")

    async def run(self) -> None:
        log.info("Reflection scheduler started")
        await self._maybe_refresh()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=CHECK_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass
            await self._maybe_refresh()

    def stop(self) -> None:
        self._stop.set()
