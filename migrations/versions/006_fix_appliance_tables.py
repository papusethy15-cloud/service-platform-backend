"""Fix appliance tables — add missing columns to match current models

Revision ID: 006_fix_appliance_tables
Revises: 005_add_domain_id_columns
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '006_fix_appliance_tables'
down_revision = '005_domain_id_cols'
branch_labels = None
depends_on = None


def column_exists(table, column):
    from alembic import op as _op
    conn = _op.get_bind()
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column})
    return result.scalar() > 0


def table_exists(table):
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=:t"
    ), {"t": table})
    return result.scalar() > 0


def upgrade():
    # ── appliance_brands: add logo_url ────────────────────────────
    if not column_exists('appliance_brands', 'logo_url'):
        op.add_column('appliance_brands',
            sa.Column('logo_url', sa.String(500), nullable=True))

    # ── appliance_types: add brand_id + category string ───────────
    # Old migration used category_id (FK to service_categories)
    # New model uses: category (String), brand_id (FK to appliance_brands)
    if not column_exists('appliance_types', 'brand_id'):
        op.add_column('appliance_types',
            sa.Column('brand_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('appliance_brands.id'), nullable=True))
    if not column_exists('appliance_types', 'category'):
        op.add_column('appliance_types',
            sa.Column('category', sa.String(100), nullable=True))

    # ── customer_appliances: add all missing columns ───────────────
    appliance_cols = {
        'serial_number':      sa.Column('serial_number',     sa.String(200), nullable=True),
        'category':           sa.Column('category',          sa.String(100), nullable=True),
        'model':              sa.Column('model',             sa.String(200), nullable=True),
        'warranty_expiry':    sa.Column('warranty_expiry',   sa.DateTime(timezone=True), nullable=True),
        'installation_date':  sa.Column('installation_date', sa.DateTime(timezone=True), nullable=True),
        'purchase_date_tz':   None,   # handled separately — old col is Date, we keep it
        'status':             sa.Column('status',            sa.String(30), nullable=True, server_default='ACTIVE'),
        'notes':              sa.Column('notes',             sa.Text(), nullable=True),
        'image_url':          sa.Column('image_url',         sa.String(500), nullable=True),
    }
    for col_name, col_def in appliance_cols.items():
        if col_name == 'purchase_date_tz':
            continue
        if not column_exists('customer_appliances', col_name):
            op.add_column('customer_appliances', col_def)

    # Rename model_name → model if model_name exists and model doesn't
    if column_exists('customer_appliances', 'model_name') and not column_exists('customer_appliances', 'model'):
        op.alter_column('customer_appliances', 'model_name', new_column_name='model')

    # purchase_date: old col is Date — add purchase_date as DateTime if missing
    if not column_exists('customer_appliances', 'purchase_date'):
        op.add_column('customer_appliances',
            sa.Column('purchase_date', sa.DateTime(timezone=True), nullable=True))

    # ── appliance_service_history table ───────────────────────────
    if not table_exists('appliance_service_history'):
        op.create_table(
            'appliance_service_history',
            sa.Column('id',             postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column('appliance_id',   postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('customer_appliances.id'), nullable=False),
            sa.Column('booking_id',     postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('bookings.id'), nullable=True),
            sa.Column('service_date',   sa.DateTime(timezone=True), nullable=True),
            sa.Column('issue_reported', sa.Text(), nullable=True),
            sa.Column('work_done',      sa.Text(), nullable=True),
            sa.Column('technician_id',  postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('technicians.id'), nullable=True),
            sa.Column('created_at',     sa.DateTime(timezone=True),
                      server_default=sa.text('now()'), nullable=False),
        )
        op.create_index('ix_ash_appliance', 'appliance_service_history', ['appliance_id'])
        op.create_index('ix_ash_booking',   'appliance_service_history', ['booking_id'])


def downgrade():
    pass  # irreversible — data safety
