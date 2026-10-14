"""Session guides table — operational lessons learned at session end.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_guides",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("learnings", sa.Text(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(learnings) > 0", name="guide_learnings_not_empty"),
        sa.UniqueConstraint("conversation_id", name="guide_one_per_conversation"),
    )
    op.create_index("guide_user_created", "session_guides", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("guide_user_created", table_name="session_guides")
    op.drop_table("session_guides")
