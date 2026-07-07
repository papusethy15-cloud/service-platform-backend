"""Merge inventory/technician chain with booking chain into single head

Revision ID: 029
Revises: 028, a1b2c3d4e5f6
Create Date: 2026-06-27
"""
from alembic import op

revision = '029'
down_revision = ('028', 'a1b2c3d4e5f6')
branch_labels = None
depends_on = None


def upgrade():
    pass  # merge-only, no schema changes


def downgrade():
    pass
