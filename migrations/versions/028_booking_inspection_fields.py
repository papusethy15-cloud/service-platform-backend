"""Add inspection_notes and inspection_photos to bookings

Revision ID: 028
Revises: 027_technician_captain_fields
Create Date: 2026-06-27
"""
from alembic import op
import sqlalchemy as sa

revision = '028'
down_revision = '027'
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    bind.execute(sa.text("""
        ALTER TABLE bookings
            ADD COLUMN IF NOT EXISTS inspection_notes  TEXT,
            ADD COLUMN IF NOT EXISTS inspection_photos TEXT
    """))

def downgrade():
    op.drop_column('bookings', 'inspection_photos')
    op.drop_column('bookings', 'inspection_notes')
