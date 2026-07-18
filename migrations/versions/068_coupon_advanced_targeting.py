"""068 coupon advanced targeting

Revision ID: 068_coupon_advanced_targeting
Revises: 067
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = '068'
down_revision = '067'
branch_labels = None
depends_on = None

def upgrade():
    from sqlalchemy import text
    op.execute(text("ALTER TABLE coupons ADD COLUMN IF NOT EXISTS customer_mobile_numbers TEXT[]"))
    op.execute(text("ALTER TABLE coupons ADD COLUMN IF NOT EXISTS service_ids TEXT[]"))
    op.execute(text("ALTER TABLE coupons ADD COLUMN IF NOT EXISTS category_ids TEXT[]"))
    op.execute(text("ALTER TABLE coupons ADD COLUMN IF NOT EXISTS per_customer_limit INTEGER"))

def downgrade():
    op.drop_column('coupons', 'customer_mobile_numbers')
    op.drop_column('coupons', 'service_ids')
    op.drop_column('coupons', 'category_ids')
    op.drop_column('coupons', 'per_customer_limit')
