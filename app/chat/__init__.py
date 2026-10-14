"""Conversation layer: turn execution, session lifecycle, and Telegram frontend.

  service.py        run_turn / run_turn_stream — one request from start to finish
  session.py        session-end processing (extract memories, self-learning guide)
  session_manager.py  background task: timeout detection, crash recovery
  telegram.py       Telegram long-poll bot (the main user interface)
"""
