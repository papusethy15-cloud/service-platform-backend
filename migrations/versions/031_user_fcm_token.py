"""Add fcm_token to users table

Revision ID: 031
Revises: 030
Create Date: 2026-06-28
"""
from alembic import op
import sqlalchemy as sa

revision = '031'
down_revision = '030'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('users', sa.Column('fcm_token', sa.String(500), nullable=True))

def downgrade():
    op.drop_column('users', 'fcm_token')
