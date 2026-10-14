"""Observability: traces, spans, logs and the queries behind /dashboard.

Recording must never break the assistant: every write path swallows its own errors.
"""
