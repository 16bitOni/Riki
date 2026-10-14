"""FastAPI app.   Run:  uvicorn app.main:app --reload"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.obs_routes import router as obs_router
from app.api.routes import router
from app.core.checkpoint import close_checkpointer, init_checkpointer
from app.config import settings
from app.core.db import assert_embedding_dimension, engine, get_or_create_user
from app.agents.graph import init_graph
from app.obs import health as obs_health
from app.obs import logs as obs_logs
from app.cognition import CognitionScheduler
from app.features.nudge_scheduler import NudgeScheduler
from app.cognition import ReflectionScheduler
from app.features.reminders import ReminderScheduler
from app.chat.session_manager import SessionManager
from app.core.startup_check import chat_warning, log_summary, run_all as run_startup_checks
from app.chat.telegram import start_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
for noisy in ("httpx", "httpcore", "langchain_aws", "langgraph", "botocore", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
obs_logs.install()  # keeps recent log lines in memory for the dashboard

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRONTEND = FRONTEND_DIR / "index.html"


_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("Starting up …")
    await get_or_create_user()
    await assert_embedding_dimension()

    init_checkpointer()
    init_graph()

    session_mgr = SessionManager()
    reflection_scheduler = ReflectionScheduler()
    telegram = start_bot()
    notify_fn = telegram[1].notify if telegram else None
    cognition_scheduler = CognitionScheduler(notify_fn=notify_fn)
    nudge_scheduler: NudgeScheduler | None = None
    background = [
        asyncio.create_task(obs_health.sample_loop_lag()),
        asyncio.create_task(session_mgr.run()),
        asyncio.create_task(reflection_scheduler.run()),
        asyncio.create_task(cognition_scheduler.run()),
    ]
    if telegram:
        scheduler = ReminderScheduler(telegram[1].notify)
        nudge_scheduler = NudgeScheduler(telegram[1].notify)
        background.append(asyncio.create_task(scheduler.run()))
        background.append(asyncio.create_task(nudge_scheduler.run()))
    if settings.telemetry_enabled:
        background.append(asyncio.create_task(obs_health.purge_loop()))

    _log.info(
        "Ready  model=%s  telegram=%s  session_timeout=%dh",
        settings.main_model_id.split(".")[-1][:40],
        "on" if telegram else "off",
        settings.session_timeout_hours,
    )

    _log.info("Running startup checks …")
    check_results = await run_startup_checks()
    log_summary(check_results)
    warning = chat_warning(check_results)
    if warning and telegram:
        try:
            await telegram[1].notify(warning)
        except Exception:
            _log.warning("Could not send startup warning via Telegram")
    yield
    _log.info("Shutting down …")
    session_mgr.stop()
    reflection_scheduler.stop()
    cognition_scheduler.stop()
    if nudge_scheduler:
        nudge_scheduler.stop()
    try:
        await asyncio.wait_for(session_mgr.shutdown_close_active(), timeout=30)
    except asyncio.TimeoutError:
        _log.warning("Session shutdown timed out after 30s")
    for task in background:
        task.cancel()
    if telegram:
        task, bot = telegram
        task.cancel()
        await bot.close()
    close_checkpointer()
    await engine.dispose()
    _log.info("Shutdown complete")


app = FastAPI(title="Personal Agent", lifespan=lifespan)
app.include_router(router)
app.include_router(obs_router)
app.mount("/dashboard", StaticFiles(directory=FRONTEND_DIR / "dashboard", html=True), name="dashboard")

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(FRONTEND)
