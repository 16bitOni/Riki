"""Reminders table.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message", sa.String(300), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'sent', 'cancelled')", name="reminder_status_valid"),
        sa.CheckConstraint("length(message) > 0", name="reminder_message_not_empty"),
    )
    op.create_index(
        "reminder_pending_due", "reminders", ["due_at"], postgresql_where=sa.text("status = 'pending'")
    )


def downgrade() -> None:
    op.drop_index("reminder_pending_due", table_name="reminders")
    op.drop_table("reminders")
