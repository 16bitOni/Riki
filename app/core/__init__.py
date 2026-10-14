"""Core infrastructure: database, models, schemas, LLM client, storage, utilities.

These modules are imported by almost everything else — they form the foundation
the rest of the app is built on.

  db.py           async SQLAlchemy session + get_or_create_user
  models.py       all SQLAlchemy table definitions
  schemas.py      Pydantic types shared across the app
  llm.py          Bedrock chat model + embedding client
  storage.py      archive backend (local folder or S3)
  checkpoint.py   LangGraph postgres checkpointer
  timeutil.py     timezone-aware datetime helpers
  privacy.py      PII scrubbing before sending to the model
  startup_check.py  validates config + external connections at boot
"""
