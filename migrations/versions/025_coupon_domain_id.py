"""025 - add domain_id to coupons table

Revision ID: 025
Revises: 024_domain_cities
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '025'
down_revision = '024_domain_cities'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Drop old global unique constraint on code (if it exists by that name)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'coupons_code_key'
            ) THEN
                ALTER TABLE coupons DROP CONSTRAINT coupons_code_key;
            END IF;
        END $$;
    """)

    # 2. Add domain_id column idempotently
    op.execute("""
        ALTER TABLE coupons
            ADD COLUMN IF NOT EXISTS domain_id UUID REFERENCES domains(id) ON DELETE SET NULL
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_coupons_domain_id ON coupons (domain_id)")

    # 3. Unique indexes — idempotent
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_coupon_code_domain
        ON coupons (code, domain_id)
        WHERE domain_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_coupon_code_global
        ON coupons (code)
        WHERE domain_id IS NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_coupon_code_domain;")
    op.execute("DROP INDEX IF EXISTS uq_coupon_code_global;")
    op.drop_index('ix_coupons_domain_id', table_name='coupons')
    op.drop_column('coupons', 'domain_id')
    # Restore original global unique constraint
    op.create_unique_constraint('coupons_code_key', 'coupons', ['code'])
