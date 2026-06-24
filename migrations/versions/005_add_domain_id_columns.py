"""Add domain_id to bookings/quotations/invoices, add missing city tables

Revision ID: 005_domain_id_cols
Revises: 004_domain_tables
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '005_domain_id_cols'
down_revision = '004_domain_tables'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    def col_exists(table, column):
        return bind.execute(sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{table}' AND column_name='{column}')"
        )).scalar()

    def table_exists(name):
        return bind.execute(sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema='public' AND table_name='{name}')"
        )).scalar()

    # ── Add domain_id to bookings ───────────────────────────
    if not col_exists('bookings', 'domain_id'):
        op.add_column('bookings',
            sa.Column('domain_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('domains.id'), nullable=True))

    # ── Add domain_id to quotations ─────────────────────────
    if not col_exists('quotations', 'domain_id'):
        op.add_column('quotations',
            sa.Column('domain_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('domains.id'), nullable=True))

    # ── Add domain_id to invoices ───────────────────────────
    if not col_exists('invoices', 'domain_id'):
        op.add_column('invoices',
            sa.Column('domain_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('domains.id'), nullable=True))

    # ── Add missing status values to bookingstatus enum ─────
    # The model added new statuses; add them safely
    new_statuses = [
        'PENDING_VERIFICATION', 'TECHNICIAN_ACCEPTED', 'INVOICE_GENERATED',
        'PAYMENT_PENDING', 'WORK_STARTED', 'WORK_PAUSED', 'REFUND_INITIATED'
    ]
    for s in new_statuses:
        exists = bind.execute(sa.text(
            f"SELECT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel='{s}' "
            "AND enumtypid = (SELECT oid FROM pg_type WHERE typname='bookingstatus'))"
        )).scalar()
        if not exists:
            bind.execute(sa.text(
                f"ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS '{s}'"
            ))

    # ── Zones table ─────────────────────────────────────────
    if not table_exists('zones'):
        op.create_table('zones',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('city_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('cities.id'), nullable=False),
            sa.Column('name', sa.String(150), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default='true'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('NOW()')),
        )

    # ── City settings table ─────────────────────────────────
    if not table_exists('city_settings'):
        op.create_table('city_settings',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('city_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('cities.id'), nullable=False),
            sa.Column('min_booking_amount', sa.Float(), server_default='0.0'),
            sa.Column('max_booking_amount', sa.Float(), nullable=True),
            sa.Column('booking_advance_days', sa.Integer(), server_default='7'),
            sa.Column('cancellation_window_hrs', sa.Integer(), server_default='2'),
            sa.Column('auto_assign_enabled', sa.Boolean(), server_default='true'),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default='true'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('NOW()')),
            sa.UniqueConstraint('city_id', name='uq_city_settings'),
        )

    # ── Add zone_id to areas if missing ────────────────────
    if not col_exists('areas', 'zone_id'):
        op.add_column('areas',
            sa.Column('zone_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('zones.id'), nullable=True))
    if not col_exists('areas', 'latitude'):
        op.add_column('areas', sa.Column('latitude', sa.Float(), nullable=True))
    if not col_exists('areas', 'longitude'):
        op.add_column('areas', sa.Column('longitude', sa.Float(), nullable=True))
    if not col_exists('areas', 'surge_multiplier'):
        op.add_column('areas', sa.Column('surge_multiplier', sa.Float(), server_default='1.0'))

    # ── Add is_serviceable + lat/lon to cities if missing ───
    if not col_exists('cities', 'is_serviceable'):
        op.add_column('cities', sa.Column('is_serviceable', sa.Boolean(), server_default='true'))
    if not col_exists('cities', 'latitude'):
        op.add_column('cities', sa.Column('latitude', sa.Float(), nullable=True))
    if not col_exists('cities', 'longitude'):
        op.add_column('cities', sa.Column('longitude', sa.Float(), nullable=True))

    # ── Domain services table (if not exists) ───────────────
    if not table_exists('domain_services'):
        op.create_table('domain_services',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('domain_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('domains.id'), nullable=False),
            sa.Column('service_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('services.id'), nullable=False),
            sa.Column('is_featured', sa.Boolean(), server_default='false'),
            sa.Column('is_active', sa.Boolean(), server_default='true'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('NOW()')),
            sa.UniqueConstraint('domain_id', 'service_id', name='uq_domain_service'),
        )

    # ── Fix service_city_prices price column type (Float not String) ─
    if col_exists('service_city_prices', 'price'):
        # Check if it's varchar
        col_type = bind.execute(sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='service_city_prices' AND column_name='price'"
        )).scalar()
        if col_type and 'char' in col_type.lower():
            op.alter_column('service_city_prices', 'price',
                type_=sa.Float(), postgresql_using='price::float')


def downgrade():
    # Only drop columns added, not tables (too destructive)
    op.drop_column('bookings', 'domain_id')
    op.drop_column('quotations', 'domain_id')
    op.drop_column('invoices', 'domain_id')
