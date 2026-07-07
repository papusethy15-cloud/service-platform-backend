"""Add firebase_uid to users table for Google/Firebase auth linking

Revision ID: 032
Revises: 031
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = '032'
down_revision = '031'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('users', sa.Column('firebase_uid', sa.String(128), nullable=True))
    op.create_unique_constraint('uq_users_firebase_uid', 'users', ['firebase_uid'])

def downgrade():
    op.drop_constraint('uq_users_firebase_uid', 'users', type_='unique')
    op.drop_column('users', 'firebase_uid')
