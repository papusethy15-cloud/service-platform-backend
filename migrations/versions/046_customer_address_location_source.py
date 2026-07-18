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
    op.execute(sa.text(
        "ALTER TABLE customer_addresses ADD COLUMN IF NOT EXISTS location_source VARCHAR(50)"
    ))


def downgrade():
    op.drop_column('customer_addresses', 'location_source')
