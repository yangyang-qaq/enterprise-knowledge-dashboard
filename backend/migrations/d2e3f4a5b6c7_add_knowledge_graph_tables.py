"""add knowledge graph tables

Revision ID: d2e3f4a5b6c7
Revises: b8c9d0e1f2a3
Create Date: 2026-09-11 12:00:00.000000

Phase 12 (Graph-Enhanced RAG): stores the entity/relation graph extracted from
knowledge base chunks, the chunk<->entity mapping used for multi-hop retrieval
expansion, and the build job / per-chunk ledger that drives incremental builds.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # -- knowledge_graph_entity table --
    # Created first: edge and chunk_entity both reference it.
    if 'knowledge_graph_entity' not in existing_tables:
        op.create_table(
            'knowledge_graph_entity',
            sa.Column('id', sa.Text(), nullable=False),
            sa.Column('knowledge_id', sa.Text(), nullable=False),
            sa.Column('name', sa.Text(), nullable=False),
            sa.Column('name_key', sa.Text(), nullable=False),
            sa.Column('canonical_id', sa.Text(), nullable=True),
            sa.Column('entity_type', sa.Text(), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('aliases', sa.JSON(), nullable=True),
            sa.Column('chunk_hashes', sa.JSON(), nullable=True),
            sa.Column('mention_count', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('degree', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('knowledge_id', 'name_key', name='uq_knowledge_graph_entity_kb_name_key'),
            sa.ForeignKeyConstraint(['knowledge_id'], ['knowledge.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['canonical_id'], ['knowledge_graph_entity.id'], ondelete='SET NULL'),
        )
        op.create_index('ix_knowledge_graph_entity_knowledge_id', 'knowledge_graph_entity', ['knowledge_id'])
        op.create_index('ix_knowledge_graph_entity_canonical_id', 'knowledge_graph_entity', ['canonical_id'])
        op.create_index('ix_knowledge_graph_entity_degree', 'knowledge_graph_entity', ['knowledge_id', 'degree'])

    # -- knowledge_graph_edge table --
    if 'knowledge_graph_edge' not in existing_tables:
        op.create_table(
            'knowledge_graph_edge',
            sa.Column('id', sa.Text(), nullable=False),
            sa.Column('knowledge_id', sa.Text(), nullable=False),
            sa.Column('source_entity_id', sa.Text(), nullable=False),
            sa.Column('target_entity_id', sa.Text(), nullable=False),
            sa.Column('relation', sa.Text(), nullable=False),
            sa.Column('relation_key', sa.Text(), nullable=False),
            sa.Column('evidence_chunk_hashes', sa.JSON(), nullable=True),
            sa.Column('evidence_file_ids', sa.JSON(), nullable=True),
            sa.Column('weight', sa.BigInteger(), nullable=False, server_default='1'),
            sa.Column('is_cross_doc', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('confidence', sa.BigInteger(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint(
                'knowledge_id',
                'source_entity_id',
                'target_entity_id',
                'relation_key',
                name='uq_knowledge_graph_edge_triple',
            ),
            sa.ForeignKeyConstraint(['knowledge_id'], ['knowledge.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['source_entity_id'], ['knowledge_graph_entity.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['target_entity_id'], ['knowledge_graph_entity.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_knowledge_graph_edge_knowledge_id', 'knowledge_graph_edge', ['knowledge_id'])
        op.create_index('ix_knowledge_graph_edge_source', 'knowledge_graph_edge', ['knowledge_id', 'source_entity_id'])
        op.create_index('ix_knowledge_graph_edge_target', 'knowledge_graph_edge', ['knowledge_id', 'target_entity_id'])

    # -- knowledge_graph_chunk_entity table --
    if 'knowledge_graph_chunk_entity' not in existing_tables:
        op.create_table(
            'knowledge_graph_chunk_entity',
            sa.Column('id', sa.Text(), nullable=False),
            sa.Column('knowledge_id', sa.Text(), nullable=False),
            sa.Column('chunk_hash', sa.Text(), nullable=False),
            sa.Column('entity_id', sa.Text(), nullable=False),
            sa.Column('file_id', sa.Text(), nullable=True),
            sa.Column('chunk_index', sa.BigInteger(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('knowledge_id', 'chunk_hash', 'entity_id', name='uq_knowledge_graph_chunk_entity'),
            sa.ForeignKeyConstraint(['knowledge_id'], ['knowledge.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['entity_id'], ['knowledge_graph_entity.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_knowledge_graph_chunk_entity_entity', 'knowledge_graph_chunk_entity', ['knowledge_id', 'entity_id'])
        op.create_index('ix_knowledge_graph_chunk_entity_chunk', 'knowledge_graph_chunk_entity', ['knowledge_id', 'chunk_hash'])

    # -- knowledge_graph_extraction_task table --
    if 'knowledge_graph_extraction_task' not in existing_tables:
        op.create_table(
            'knowledge_graph_extraction_task',
            sa.Column('id', sa.Text(), nullable=False),
            sa.Column('knowledge_id', sa.Text(), nullable=False),
            sa.Column('status', sa.Text(), nullable=False),
            sa.Column('total_chunks', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('processed_chunks', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('failed_chunks', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('skipped_chunks', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('entity_count', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('edge_count', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('model', sa.Text(), nullable=True),
            sa.Column('concurrency', sa.BigInteger(), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['knowledge_id'], ['knowledge.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_knowledge_graph_task_knowledge_id', 'knowledge_graph_extraction_task', ['knowledge_id'])

    # -- knowledge_graph_extraction_log table --
    if 'knowledge_graph_extraction_log' not in existing_tables:
        op.create_table(
            'knowledge_graph_extraction_log',
            sa.Column('id', sa.Text(), nullable=False),
            sa.Column('knowledge_id', sa.Text(), nullable=False),
            sa.Column('chunk_hash', sa.Text(), nullable=False),
            sa.Column('status', sa.Text(), nullable=False),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('entity_count', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('edge_count', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('extracted_at', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('knowledge_id', 'chunk_hash', name='uq_knowledge_graph_extraction_log_chunk'),
            sa.ForeignKeyConstraint(['knowledge_id'], ['knowledge.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_knowledge_graph_extraction_log_knowledge_id', 'knowledge_graph_extraction_log', ['knowledge_id'])


def downgrade() -> None:
    # Reverse creation order so FK targets are dropped last.
    op.drop_index('ix_knowledge_graph_extraction_log_knowledge_id', table_name='knowledge_graph_extraction_log')
    op.drop_table('knowledge_graph_extraction_log')

    op.drop_index('ix_knowledge_graph_task_knowledge_id', table_name='knowledge_graph_extraction_task')
    op.drop_table('knowledge_graph_extraction_task')

    op.drop_index('ix_knowledge_graph_chunk_entity_chunk', table_name='knowledge_graph_chunk_entity')
    op.drop_index('ix_knowledge_graph_chunk_entity_entity', table_name='knowledge_graph_chunk_entity')
    op.drop_table('knowledge_graph_chunk_entity')

    op.drop_index('ix_knowledge_graph_edge_target', table_name='knowledge_graph_edge')
    op.drop_index('ix_knowledge_graph_edge_source', table_name='knowledge_graph_edge')
    op.drop_index('ix_knowledge_graph_edge_knowledge_id', table_name='knowledge_graph_edge')
    op.drop_table('knowledge_graph_edge')

    op.drop_index('ix_knowledge_graph_entity_degree', table_name='knowledge_graph_entity')
    op.drop_index('ix_knowledge_graph_entity_canonical_id', table_name='knowledge_graph_entity')
    op.drop_index('ix_knowledge_graph_entity_knowledge_id', table_name='knowledge_graph_entity')
    op.drop_table('knowledge_graph_entity')
