"""Add telegram_message_id and resolved_at to proactive_nudges for reaction tracking.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("proactive_nudges", sa.Column("telegram_message_id", sa.Integer(), nullable=True))
    op.add_column("proactive_nudges", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("nudge_telegram_msg", "proactive_nudges", ["telegram_message_id"])


def downgrade() -> None:
    op.drop_index("nudge_telegram_msg", table_name="proactive_nudges")
    op.drop_column("proactive_nudges", "resolved_at")
    op.drop_column("proactive_nudges", "telegram_message_id")
