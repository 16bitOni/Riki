"""Initial schema: users, chat, memories (with full-text and vector indexes), graph, tasks, notes.

Revision ID: 0001
Revises: 
"""
from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('profile', sa.Text(), nullable=False),
    sa.Column('profile_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('conversations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('conversation_user_created', 'conversations', ['user_id', 'created_at'], unique=False)
    op.create_table('entities',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('type', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("type IN ('person', 'place', 'org', 'project', 'other')", name='entity_type_valid'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('entity_user_name_unique', 'entities', ['user_id', sa.literal_column('lower(name)')], unique=True)
    op.create_table('memories',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('importance', sa.Integer(), nullable=False),
    sa.Column('event_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_key', sa.String(length=512), nullable=True),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('superseded_by_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('fact', 'preference', 'event', 'episode')", name='memory_kind_valid'),
    sa.CheckConstraint('importance BETWEEN 1 AND 5', name='memory_importance_range'),
    sa.CheckConstraint('length(content) > 0', name='memory_content_not_empty'),
    sa.ForeignKeyConstraint(['superseded_by_id'], ['memories.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'content_hash', name='memory_user_content_unique')
    )
    op.create_index('memory_embedding_idx', 'memories', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('memory_fts_idx', 'memories', [sa.literal_column("to_tsvector('english', content)")], unique=False, postgresql_using='gin')
    op.create_index('memory_source_key', 'memories', ['source_key'], unique=False)
    op.create_index('memory_user_active_created', 'memories', ['user_id', 'is_active', 'created_at'], unique=False)
    op.create_index('memory_user_event', 'memories', ['user_id', 'event_at'], unique=False)
    op.create_table('notes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('length(title) > 0', name='note_title_not_empty'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('note_user_created', 'notes', ['user_id', 'created_at'], unique=False)
    op.create_table('tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('details', sa.Text(), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('open', 'done')", name='task_status_valid'),
    sa.CheckConstraint('length(title) > 0', name='task_title_not_empty'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('task_user_status_due', 'tasks', ['user_id', 'status', 'due_at'], unique=False)
    op.create_table('entity_relations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('subject_id', sa.Uuid(), nullable=False),
    sa.Column('predicate', sa.String(length=100), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['subject_id'], ['entities.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_id'], ['entities.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('subject_id', 'predicate', 'target_id', name='relation_unique')
    )
    op.create_index('relation_target', 'entity_relations', ['target_id'], unique=False)
    op.create_table('memory_entities',
    sa.Column('memory_id', sa.Uuid(), nullable=False),
    sa.Column('entity_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['memory_id'], ['memories.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('memory_id', 'entity_id')
    )
    op.create_index('memory_entity_entity', 'memory_entities', ['entity_id'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("role IN ('user', 'assistant')", name='message_role_valid'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('message_conversation_created', 'messages', ['conversation_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('message_conversation_created', table_name='messages')
    op.drop_table('messages')
    op.drop_index('memory_entity_entity', table_name='memory_entities')
    op.drop_table('memory_entities')
    op.drop_index('relation_target', table_name='entity_relations')
    op.drop_table('entity_relations')
    op.drop_index('task_user_status_due', table_name='tasks')
    op.drop_table('tasks')
    op.drop_index('note_user_created', table_name='notes')
    op.drop_table('notes')
    op.drop_index('memory_user_event', table_name='memories')
    op.drop_index('memory_user_active_created', table_name='memories')
    op.drop_index('memory_source_key', table_name='memories')
    op.drop_index('memory_fts_idx', table_name='memories', postgresql_using='gin')
    op.drop_index('memory_embedding_idx', table_name='memories', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_table('memories')
    op.drop_index('entity_user_name_unique', table_name='entities')
    op.drop_table('entities')
    op.drop_index('conversation_user_created', table_name='conversations')
    op.drop_table('conversations')
    op.drop_table('users')
