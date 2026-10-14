"""Cognition cycles: tracks each time the agent thinks between conversations.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cognition_cycles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reflection_id", sa.Uuid(), sa.ForeignKey("user_reflections.id", ondelete="SET NULL"), nullable=True),
        sa.Column("memories_reviewed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notices", JSONB(), nullable=False, server_default="[]"),
        sa.Column("monologue", sa.Text(), nullable=False, server_default=""),
        sa.Column("reached_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reach_out_message", sa.Text(), nullable=True),
        sa.Column("reach_out_topic", sa.String(200), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("cognition_user_created", "cognition_cycles", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("cognition_user_created", table_name="cognition_cycles")
    op.drop_table("cognition_cycles")
