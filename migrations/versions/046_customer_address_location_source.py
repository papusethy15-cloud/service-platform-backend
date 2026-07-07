"""Add location_source to customer_addresses

Revision ID: 046
Revises: 045
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa

revision = '046'
down_revision = '045'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'customer_addresses',
        sa.Column('location_source', sa.String(50), nullable=True, comment="'gps'|'whatsapp'|'manual'|'geocoded'"),
    )


def downgrade():
    op.drop_column('customer_addresses', 'location_source')
