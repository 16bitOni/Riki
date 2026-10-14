"""Embedding drift protection and near-duplicate detection.

- memories.wants_embedding / embedding_model / embedded_at: know which model made each vector,
  so vectors from different models are never compared, and failed embeddings can be redone.
- pg_trgm + a trigram index on content: cheap near-duplicate detection for every memory.

Existing vectors are labelled with the default model (amazon.titan-embed-text-v2:0), which is
what every version of this project has used so far.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DEFAULT_MODEL = "amazon.titan-embed-text-v2:0"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.add_column(
        "memories",
        sa.Column("wants_embedding", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("memories", sa.Column("embedding_model", sa.String(length=100), nullable=True))
    op.add_column("memories", sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True))

    # label vectors that already exist
    op.execute(
        sa.text(
            "UPDATE memories SET wants_embedding = true, embedding_model = :model, embedded_at = updated_at "
            "WHERE embedding IS NOT NULL"
        ).bindparams(model=DEFAULT_MODEL)
    )

    op.create_check_constraint(
        "memory_vector_names_its_model", "memories", "embedding IS NULL OR embedding_model IS NOT NULL"
    )
    op.create_index(
        "memory_content_trgm_idx",
        "memories",
        ["content"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("memory_content_trgm_idx", table_name="memories")
    op.drop_constraint("memory_vector_names_its_model", "memories", type_="check")
    op.drop_column("memories", "embedded_at")
    op.drop_column("memories", "embedding_model")
    op.drop_column("memories", "wants_embedding")
