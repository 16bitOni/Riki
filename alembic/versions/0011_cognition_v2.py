"""Cognition v2: user state model, attention threads, feedback columns.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- user state model
    op.create_table(
        "user_states",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("dimensions", JSONB(), nullable=False, server_default="{}"),
        sa.Column("signals_snapshot", JSONB(), nullable=False, server_default="{}"),
        sa.Column("diff_from_previous", JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("state_user_computed", "user_states", ["user_id", "computed_at"])

    # ---- attention threads
    op.create_table(
        "attention_threads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("topic", sa.String(300), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="watching"),
        sa.Column("hypothesis", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", JSONB(), nullable=False, server_default="[]"),
        sa.Column("trigger", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("source_cycle_id", sa.Uuid(), sa.ForeignKey("cognition_cycles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resolve_after_cycles", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("cycles_without_update", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("was_accurate", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('watching', 'resolved', 'abandoned')", name="thread_status_valid"),
    )
    op.create_index("thread_user_status", "attention_threads", ["user_id", "status"])
    op.create_index("thread_user_created", "attention_threads", ["user_id", "created_at"])

    # ---- new columns on existing tables
    op.add_column("proactive_nudges", sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("proactive_nudges", sa.Column(
        "attention_thread_id", sa.Uuid(),
        sa.ForeignKey("attention_threads.id", ondelete="SET NULL"), nullable=True,
    ))
    op.add_column("cognition_cycles", sa.Column("phase_durations", JSONB(), nullable=False, server_default="{}"))
    op.add_column("cognition_cycles", sa.Column("signals_summary", JSONB(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("cognition_cycles", "signals_summary")
    op.drop_column("cognition_cycles", "phase_durations")
    op.drop_column("proactive_nudges", "attention_thread_id")
    op.drop_column("proactive_nudges", "responded_at")
    op.drop_index("thread_user_created", table_name="attention_threads")
    op.drop_index("thread_user_status", table_name="attention_threads")
    op.drop_table("attention_threads")
    op.drop_index("state_user_computed", table_name="user_states")
    op.drop_table("user_states")
