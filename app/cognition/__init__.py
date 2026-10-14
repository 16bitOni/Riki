"""Cognition: the agent's inner life between conversations.

Subpackages and their roles:
  cycle.py              — the 4-phase thinking cycle (compute → think → update → act)
  scheduler.py          — background scheduler (runs a cycle every N hours)
  reflection.py         — generates and stores the reflection document
  reflection_scheduler.py — keeps the reflection fresh (legacy, runs alongside cognition)
  signals.py            — pure SQL signal computation (entity frequency, habits, cadence, …)
  state.py              — structured user state model (mood, energy, focus, open loops)
  attention.py          — attention threads (tracked hypotheses with lifecycle)
"""

from app.cognition.attention import (
    ThreadAssessment,
    abandon_stale_threads,
    add_evidence,
    create_thread,
    format_threads_for_prompt,
    get_active_threads,
    get_thread_accuracy_stats,
    increment_stale_counters,
    resolve_thread,
)
from app.cognition.cycle import ActOutput, ThinkOutput, run_cognition_cycle
from app.cognition.reflection import (
    generate_reflection,
    get_latest_reflection,
    get_reflection_context,
    refresh_reflection_if_stale,
)
from app.cognition.reflection_scheduler import ReflectionScheduler
from app.cognition.scheduler import CognitionScheduler
from app.cognition.signals import compute_all_signals
from app.cognition.state import (
    UserDimensions,
    format_diff_for_prompt,
    format_state_for_prompt,
    get_latest_state,
    save_state,
)

__all__ = [
    # cycle
    "run_cognition_cycle",
    "ThinkOutput",
    "ActOutput",
    # schedulers
    "CognitionScheduler",
    "ReflectionScheduler",
    # reflection
    "generate_reflection",
    "get_latest_reflection",
    "get_reflection_context",
    "refresh_reflection_if_stale",
    # signals
    "compute_all_signals",
    # state
    "UserDimensions",
    "format_diff_for_prompt",
    "format_state_for_prompt",
    "get_latest_state",
    "save_state",
    # attention
    "ThreadAssessment",
    "abandon_stale_threads",
    "add_evidence",
    "create_thread",
    "format_threads_for_prompt",
    "get_active_threads",
    "get_thread_accuracy_stats",
    "increment_stale_counters",
    "resolve_thread",
]
