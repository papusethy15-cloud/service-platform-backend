"""Add brand_categories join table

Revision ID: 007_add_brand_categories
Revises: 006_fix_appliance_tables
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '007_add_brand_categories'
down_revision = '006_fix_appliance_tables'
branch_labels = None
depends_on = None


def table_exists(name):
    conn = op.get_bind()
    r = conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=:t"
    ), {"t": name})
    return r.scalar() > 0


def upgrade():
    if not table_exists('brand_categories'):
        op.create_table(
            'brand_categories',
            sa.Column('id',                    postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column('brand_id',              postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('appliance_brands.id', ondelete='CASCADE'), nullable=False),
            sa.Column('appliance_category_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('service_categories.id', ondelete='CASCADE'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.UniqueConstraint('brand_id', 'appliance_category_id', name='uq_brand_category'),
        )
        op.create_index('ix_brand_cat_brand', 'brand_categories', ['brand_id'])
        op.create_index('ix_brand_cat_cat',   'brand_categories', ['appliance_category_id'])


def downgrade():
    op.drop_table('brand_categories')
