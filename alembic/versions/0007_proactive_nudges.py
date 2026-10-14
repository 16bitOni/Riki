"""Proactive nudges table — messages Riki sends without being asked.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proactive_nudges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("topic", sa.String(200), nullable=False, server_default=""),
        sa.Column("kind", sa.String(20), nullable=False, server_default="open_loop"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("nudge_user_sent", "proactive_nudges", ["user_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("nudge_user_sent", table_name="proactive_nudges")
    op.drop_table("proactive_nudges")
