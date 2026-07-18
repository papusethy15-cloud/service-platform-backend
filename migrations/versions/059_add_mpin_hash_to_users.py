"""add mpin_hash to users

Revision ID: 059
Revises: 058
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text, inspect

revision = '059'
down_revision = '058'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS mpin_hash VARCHAR(255)"))

def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS mpin_hash"))
