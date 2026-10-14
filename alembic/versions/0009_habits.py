"""Habit tracking tables.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "habits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("frequency", sa.String(20), nullable=False, server_default="daily"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "name", name="habit_unique_name"),
        sa.CheckConstraint("frequency IN ('daily', 'weekday', 'weekly')", name="habit_frequency_check"),
    )
    op.create_index("habit_user_active", "habits", ["user_id", "active"])

    op.create_table(
        "habit_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("habit_id", sa.Uuid(), sa.ForeignKey("habits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("value", sa.String(200), nullable=False, server_default="done"),
        sa.Column("note", sa.String(500), nullable=False, server_default=""),
        sa.Column("logged_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("entry_habit_date", "habit_entries", ["habit_id", "logged_for"])
    op.create_index("entry_user_date", "habit_entries", ["user_id", "logged_for"])


def downgrade() -> None:
    op.drop_index("entry_user_date", table_name="habit_entries")
    op.drop_index("entry_habit_date", table_name="habit_entries")
    op.drop_table("habit_entries")
    op.drop_index("habit_user_active", table_name="habits")
    op.drop_table("habits")
