"""Fix appliance_types.category_id → appliance_category_id
   and add missing appliance_category_id to customer_appliances

Revision ID: 008_fix_appliance_column_names
Revises: 007_add_brand_categories
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '008_fix_appliance_column_names'
down_revision = '007_add_brand_categories'
branch_labels = None
depends_on = None


def column_exists(table, column):
    conn = op.get_bind()
    r = conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column})
    return r.scalar() > 0


def upgrade():
    # ── appliance_types ─────────────────────────────────────────────────────
    # old column name was category_id; model now uses appliance_category_id
    if column_exists('appliance_types', 'category_id') and \
       not column_exists('appliance_types', 'appliance_category_id'):
        op.alter_column('appliance_types', 'category_id',
                        new_column_name='appliance_category_id')

    # If neither exists (002 migration skipped both), add from scratch
    if not column_exists('appliance_types', 'appliance_category_id'):
        op.add_column('appliance_types', sa.Column(
            'appliance_category_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('service_categories.id'), nullable=True
        ))

    # ── customer_appliances ──────────────────────────────────────────────────
    # add appliance_category_id FK if missing
    if not column_exists('customer_appliances', 'appliance_category_id'):
        op.add_column('customer_appliances', sa.Column(
            'appliance_category_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('service_categories.id'), nullable=True
        ))

    # ── brand_categories ─────────────────────────────────────────────────────
    # Guard: ensure brand_categories.appliance_category_id column name is correct
    # (007 created it as appliance_category_id, this just double-checks)
    if not column_exists('brand_categories', 'appliance_category_id'):
        op.add_column('brand_categories', sa.Column(
            'appliance_category_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('service_categories.id'), nullable=False
        ))


def downgrade():
    pass
