"""027 — Add Captain App fields to technicians table

Adds:
  is_online   BOOLEAN NOT NULL DEFAULT FALSE
  fcm_token   VARCHAR(500)
  last_lat    FLOAT
  last_lng    FLOAT
"""
from alembic import op
import sqlalchemy as sa

revision  = '027'
down_revision = '026_platform_settings'
branch_labels = None
depends_on    = None


def upgrade():
    bind = op.get_bind()
    bind.execute(sa.text("""
        ALTER TABLE technicians
            ADD COLUMN IF NOT EXISTS is_online BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS fcm_token VARCHAR(500),
            ADD COLUMN IF NOT EXISTS last_lat  DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS last_lng  DOUBLE PRECISION
    """))


def downgrade():
    op.drop_column('technicians', 'last_lng')
    op.drop_column('technicians', 'last_lat')
    op.drop_column('technicians', 'fcm_token')
    op.drop_column('technicians', 'is_online')
