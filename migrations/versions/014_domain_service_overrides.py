"""Add domain_service_overrides table — per-domain image and SEO for each linked service

Revision ID: 014_domain_service_overrides
Revises: 013_domain_profile
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
import uuid

revision = '014_domain_service_overrides'
down_revision = '013_domain_profile'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'domain_service_overrides',
        sa.Column('id',               UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('created_at',       sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at',       sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.Column('is_active',        sa.Boolean(), default=True, nullable=False),

        # FK to domain_services (the link row)
        sa.Column('domain_service_id', UUID(as_uuid=True),
                  sa.ForeignKey('domain_services.id', ondelete='CASCADE'), nullable=False),

        # Domain-specific image uploads
        sa.Column('image_url',        sa.String(500), nullable=True),   # main service image
        sa.Column('thumbnail_url',    sa.String(500), nullable=True),   # card thumbnail

        # Domain-specific SEO
        sa.Column('meta_title',       sa.String(200), nullable=True),
        sa.Column('meta_description', sa.Text,        nullable=True),
        sa.Column('meta_keywords',    sa.Text,        nullable=True),
        sa.Column('og_title',         sa.String(200), nullable=True),
        sa.Column('og_description',   sa.Text,        nullable=True),
        sa.Column('og_image_url',     sa.String(500), nullable=True),
        sa.Column('canonical_url',    sa.String(500), nullable=True),
        sa.Column('robots',           sa.String(100), nullable=True),
        sa.Column('schema_json',      sa.Text,        nullable=True),

        sa.UniqueConstraint('domain_service_id', name='uq_domain_service_override'),
    )
    op.create_index(
        'ix_domain_service_overrides_domain_service_id',
        'domain_service_overrides', ['domain_service_id']
    )


def downgrade():
    op.drop_index('ix_domain_service_overrides_domain_service_id', table_name='domain_service_overrides')
    op.drop_table('domain_service_overrides')
