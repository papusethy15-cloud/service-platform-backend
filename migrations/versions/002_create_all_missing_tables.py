"""create all missing tables

Revision ID: 002_all_missing
Revises: 001_add_all_missing_tables
Create Date: 2026-05-30

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '002_all_missing'
down_revision = '001_missing_tables'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    def table_exists(name):
        return bind.execute(sa.text(
            f"SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='{name}')"
        )).scalar()

    def col_exists(table, col):
        return bind.execute(sa.text(
            f"SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='{table}' AND column_name='{col}')"
        )).scalar()

    # ── CITIES ──────────────────────────────────────────────
    if not table_exists('cities'):
        op.create_table('cities',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('state', sa.String(100), nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('areas'):
        op.create_table('areas',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('city_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('cities.id'), nullable=False),
            sa.Column('name', sa.String(150), nullable=False),
            sa.Column('pincode', sa.String(10), nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── DOMAINS ──────────────────────────────────────────────
    if not table_exists('domains'):
        op.create_table('domains',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('slug', sa.String(100), unique=True, nullable=False),
            sa.Column('description', sa.Text, nullable=True),
            sa.Column('logo_url', sa.String(500), nullable=True),
            sa.Column('primary_color', sa.String(20), default='#1B4FD8'),
            sa.Column('meta_title', sa.String(200), nullable=True),
            sa.Column('meta_desc', sa.Text, nullable=True),
            sa.Column('sort_order', sa.Integer, default=0),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('domain_services'):
        op.create_table('domain_services',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('domain_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('domains.id'), nullable=False),
            sa.Column('service_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('services.id'), nullable=False),
            sa.Column('is_featured', sa.Boolean, default=False),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('service_city_prices'):
        op.create_table('service_city_prices',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('service_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('services.id'), nullable=False),
            sa.Column('city_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('cities.id'), nullable=False),
            sa.Column('price', sa.String(20), nullable=False),
            sa.Column('is_available', sa.Boolean, default=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── APPLIANCES ───────────────────────────────────────────
    if not table_exists('appliance_brands'):
        op.create_table('appliance_brands',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('appliance_types'):
        op.create_table('appliance_types',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('customer_appliances'):
        op.create_table('customer_appliances',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id'), nullable=False),
            sa.Column('appliance_type_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('brand_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('model', sa.String(100), nullable=True),
            sa.Column('serial_number', sa.String(100), nullable=True),
            sa.Column('purchase_date', sa.DateTime, nullable=True),
            sa.Column('warranty_end', sa.DateTime, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── INVENTORY ────────────────────────────────────────────
    if not table_exists('inventory_categories'):
        op.create_table('inventory_categories',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(150), nullable=False),
            sa.Column('description', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('inventory_items'):
        op.create_table('inventory_items',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('category_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('inventory_categories.id'), nullable=True),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('sku', sa.String(50), unique=True, nullable=True),
            sa.Column('unit', sa.String(20), default='piece'),
            sa.Column('cost_price', sa.Float, default=0.0),
            sa.Column('selling_price', sa.Float, default=0.0),
            sa.Column('reorder_level', sa.Integer, default=5),
            sa.Column('hsn_code', sa.String(20), nullable=True),
            sa.Column('gst_percent', sa.Float, default=18.0),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('warehouses'):
        op.create_table('warehouses',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('city_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('address', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('warehouse_stock'):
        op.create_table('warehouse_stock',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('warehouse_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('warehouses.id'), nullable=False),
            sa.Column('item_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('inventory_items.id'), nullable=False),
            sa.Column('quantity', sa.Integer, default=0),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('stock_movements'):
        op.create_table('stock_movements',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('item_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('inventory_items.id'), nullable=False),
            sa.Column('movement_type', sa.String(30), nullable=False),
            sa.Column('quantity', sa.Integer, nullable=False),
            sa.Column('from_warehouse_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('to_warehouse_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('reference_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('notes', sa.Text, nullable=True),
            sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── WALLET + COMMISSIONS ─────────────────────────────────
    if not table_exists('wallets'):
        op.create_table('wallets',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('technician_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('technicians.id'), unique=True, nullable=False),
            sa.Column('balance', sa.Float, default=0.0),
            sa.Column('total_earned', sa.Float, default=0.0),
            sa.Column('total_withdrawn', sa.Float, default=0.0),
            sa.Column('pending_amount', sa.Float, default=0.0),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('wallet_transactions'):
        op.create_table('wallet_transactions',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('wallet_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('wallets.id'), nullable=False),
            sa.Column('transaction_type', sa.String(30), nullable=False),
            sa.Column('amount', sa.Float, nullable=False),
            sa.Column('balance_before', sa.Float, default=0.0),
            sa.Column('balance_after', sa.Float, default=0.0),
            sa.Column('reference_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('notes', sa.Text, nullable=True),
            sa.Column('status', sa.String(20), default='COMPLETED'),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('commission_rules'):
        op.create_table('commission_rules',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('rule_type', sa.String(30), default='PERCENTAGE'),
            sa.Column('value', sa.Float, nullable=False),
            sa.Column('service_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('commissions'):
        op.create_table('commissions',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('technician_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('technicians.id'), nullable=False),
            sa.Column('booking_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('bookings.id'), nullable=True),
            sa.Column('rule_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('gross_amount', sa.Float, default=0.0),
            sa.Column('commission_amount', sa.Float, default=0.0),
            sa.Column('status', sa.String(20), default='PENDING'),
            sa.Column('notes', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── COUPONS ──────────────────────────────────────────────
    if not table_exists('coupons'):
        op.create_table('coupons',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('code', sa.String(50), unique=True, nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('coupon_type', sa.String(30), default='PERCENTAGE'),
            sa.Column('value', sa.Float, nullable=False),
            sa.Column('min_order_amount', sa.Float, default=0.0),
            sa.Column('max_discount_amount', sa.Float, nullable=True),
            sa.Column('usage_limit', sa.Integer, nullable=True),
            sa.Column('used_count', sa.Integer, default=0),
            sa.Column('valid_from', sa.DateTime, nullable=True),
            sa.Column('valid_until', sa.DateTime, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('coupon_usages'):
        op.create_table('coupon_usages',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('coupon_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('coupons.id'), nullable=False),
            sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id'), nullable=False),
            sa.Column('booking_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('discount_amount', sa.Float, default=0.0),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── NOTIFICATIONS ─────────────────────────────────────────
    if not table_exists('notifications'):
        op.create_table('notifications',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('title', sa.String(200), nullable=False),
            sa.Column('body', sa.Text, nullable=False),
            sa.Column('notification_type', sa.String(50), default='GENERAL'),
            sa.Column('reference_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('is_read', sa.Boolean, default=False),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('notification_templates'):
        op.create_table('notification_templates',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('event_type', sa.String(50), nullable=False),
            sa.Column('channel', sa.String(20), nullable=False),
            sa.Column('subject', sa.String(300), nullable=True),
            sa.Column('body', sa.Text, nullable=False),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── AMC ──────────────────────────────────────────────────
    if not table_exists('amc_plans'):
        op.create_table('amc_plans',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('plan_type', sa.String(30), default='GOLD'),
            sa.Column('price', sa.Float, nullable=False),
            sa.Column('duration_months', sa.Integer, default=12),
            sa.Column('visit_count', sa.Integer, nullable=False),
            sa.Column('description', sa.Text, nullable=True),
            sa.Column('appliance_types', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('amc_subscriptions'):
        op.create_table('amc_subscriptions',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id'), nullable=False),
            sa.Column('plan_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('amc_plans.id'), nullable=False),
            sa.Column('start_date', sa.DateTime, nullable=False),
            sa.Column('end_date', sa.DateTime, nullable=False),
            sa.Column('visits_remaining', sa.Integer, default=0),
            sa.Column('amount_paid', sa.Float, default=0.0),
            sa.Column('status', sa.String(20), default='ACTIVE'),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('amc_visits'):
        op.create_table('amc_visits',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('amc_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('amc_subscriptions.id'), nullable=False),
            sa.Column('scheduled_date', sa.DateTime, nullable=False),
            sa.Column('technician_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('notes', sa.Text, nullable=True),
            sa.Column('status', sa.String(20), default='SCHEDULED'),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── WARRANTY ─────────────────────────────────────────────
    if not table_exists('warranties'):
        op.create_table('warranties',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id'), nullable=False),
            sa.Column('booking_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('warranty_type', sa.String(30), default='SERVICE'),
            sa.Column('description', sa.Text, nullable=False),
            sa.Column('expiry_date', sa.DateTime, nullable=False),
            sa.Column('parts_covered', sa.Text, nullable=True),
            sa.Column('status', sa.String(20), default='ACTIVE'),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('warranty_claims'):
        op.create_table('warranty_claims',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('warranty_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('warranties.id'), nullable=False),
            sa.Column('claimed_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('booking_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('description', sa.Text, nullable=False),
            sa.Column('status', sa.String(20), default='PENDING'),
            sa.Column('approved_by', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('rejected_by', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('notes', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── CRM ──────────────────────────────────────────────────
    for tbl_name, cols in [
        ('crm_notes', [
            sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id'), nullable=False),
            sa.Column('added_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('note', sa.Text, nullable=False),
            sa.Column('note_type', sa.String(30), default='GENERAL'),
        ]),
        ('crm_followups', [
            sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id'), nullable=False),
            sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('subject', sa.String(200), nullable=False),
            sa.Column('notes', sa.Text, nullable=True),
            sa.Column('due_date', sa.DateTime, nullable=False),
            sa.Column('status', sa.String(20), default='PENDING'),
        ]),
        ('crm_tasks', [
            sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('customer_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('title', sa.String(200), nullable=False),
            sa.Column('description', sa.Text, nullable=True),
            sa.Column('due_date', sa.DateTime, nullable=True),
            sa.Column('priority', sa.String(20), default='MEDIUM'),
            sa.Column('status', sa.String(20), default='OPEN'),
        ]),
    ]:
        if not table_exists(tbl_name):
            op.create_table(tbl_name,
                sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
                *cols,
                sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
                sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
                sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
            )

    # ── ESCALATIONS ──────────────────────────────────────────
    if not table_exists('escalations'):
        op.create_table('escalations',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('booking_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('subject', sa.String(300), nullable=False),
            sa.Column('description', sa.Text, nullable=False),
            sa.Column('priority', sa.String(20), default='MEDIUM'),
            sa.Column('status', sa.String(20), default='OPEN'),
            sa.Column('assigned_to', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('resolved_by', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('resolved_at', sa.DateTime, nullable=True),
            sa.Column('resolution_notes', sa.Text, nullable=True),
            sa.Column('escalation_level', sa.Integer, default=1),
            sa.Column('escalation_notes', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── VENDORS ──────────────────────────────────────────────
    if not table_exists('vendors'):
        op.create_table('vendors',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('contact_person', sa.String(150), nullable=True),
            sa.Column('mobile', sa.String(20), nullable=True),
            sa.Column('email', sa.String(200), nullable=True),
            sa.Column('gstin', sa.String(20), nullable=True),
            sa.Column('address', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('vendor_transactions'):
        op.create_table('vendor_transactions',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('vendor_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('vendors.id'), nullable=False),
            sa.Column('amount', sa.Float, nullable=False),
            sa.Column('type', sa.String(30), nullable=False),
            sa.Column('notes', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── REFERRALS ────────────────────────────────────────────
    if not table_exists('referral_codes'):
        op.create_table('referral_codes',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), unique=True, nullable=False),
            sa.Column('code', sa.String(20), unique=True, nullable=False),
            sa.Column('total_referrals', sa.Integer, default=0),
            sa.Column('total_earned', sa.Float, default=0.0),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('referrals'):
        op.create_table('referrals',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('referrer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('referee_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('reward_amount', sa.Float, default=0.0),
            sa.Column('status', sa.String(20), default='PENDING'),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('referral_rewards'):
        op.create_table('referral_rewards',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('referral_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('amount', sa.Float, nullable=False),
            sa.Column('type', sa.String(30), default='CASH'),
            sa.Column('status', sa.String(20), default='PENDING'),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── REFUNDS ──────────────────────────────────────────────
    if not table_exists('refunds'):
        op.create_table('refunds',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('booking_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('payment_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id'), nullable=False),
            sa.Column('amount', sa.Float, nullable=False),
            sa.Column('reason', sa.Text, nullable=False),
            sa.Column('status', sa.String(20), default='PENDING'),
            sa.Column('approved_by', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('notes', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── AUDIT LOGS ───────────────────────────────────────────
    if not table_exists('audit_logs'):
        op.create_table('audit_logs',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('action', sa.String(100), nullable=False),
            sa.Column('resource', sa.String(100), nullable=True),
            sa.Column('resource_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('old_value', sa.Text, nullable=True),
            sa.Column('new_value', sa.Text, nullable=True),
            sa.Column('ip_address', sa.String(50), nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── FRANCHISE ────────────────────────────────────────────
    if not table_exists('franchises'):
        op.create_table('franchises',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('owner_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('city_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('status', sa.String(20), default='ACTIVE'),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── SLA ──────────────────────────────────────────────────
    if not table_exists('sla_policies'):
        op.create_table('sla_policies',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('response_time_minutes', sa.Integer, default=60),
            sa.Column('resolution_time_minutes', sa.Integer, default=480),
            sa.Column('priority', sa.String(20), default='NORMAL'),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── ATTENDANCE ───────────────────────────────────────────
    if not table_exists('attendance'):
        op.create_table('attendance',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('technician_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('technicians.id'), nullable=False),
            sa.Column('date', sa.DateTime, nullable=False),
            sa.Column('check_in', sa.DateTime, nullable=True),
            sa.Column('check_out', sa.DateTime, nullable=True),
            sa.Column('status', sa.String(20), default='PRESENT'),
            sa.Column('notes', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    if not table_exists('leave_requests'):
        op.create_table('leave_requests',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('technician_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('technicians.id'), nullable=False),
            sa.Column('leave_type', sa.String(30), default='CASUAL'),
            sa.Column('from_date', sa.DateTime, nullable=False),
            sa.Column('to_date', sa.DateTime, nullable=False),
            sa.Column('reason', sa.Text, nullable=True),
            sa.Column('status', sa.String(20), default='PENDING'),
            sa.Column('approved_by', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── KNOWLEDGE BASE ───────────────────────────────────────
    if not table_exists('knowledge_base_articles'):
        op.create_table('knowledge_base_articles',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('title', sa.String(300), nullable=False),
            sa.Column('content', sa.Text, nullable=False),
            sa.Column('category', sa.String(100), nullable=True),
            sa.Column('tags', sa.Text, nullable=True),
            sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )

    # ── TECHNICIAN EXTRA COLUMNS ─────────────────────────────
    for col_def in [
        ('technician_ratings', None),
    ]:
        pass  # handled by separate tables if needed

    if not table_exists('technician_ratings'):
        op.create_table('technician_ratings',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('technician_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('technicians.id'), nullable=False),
            sa.Column('booking_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('customer_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('rating', sa.Float, nullable=False),
            sa.Column('review', sa.Text, nullable=True),
            sa.Column('is_active', sa.Boolean, default=True, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
        )


def downgrade():
    for tbl in ['technician_ratings','knowledge_base_articles','leave_requests','attendance',
                'sla_policies','franchises','audit_logs','refunds','referral_rewards',
                'referrals','referral_codes','vendor_transactions','vendors',
                'escalations','crm_tasks','crm_followups','crm_notes',
                'warranty_claims','warranties','amc_visits','amc_subscriptions','amc_plans',
                'notification_templates','notifications','coupon_usages','coupons',
                'commissions','commission_rules','wallet_transactions','wallets',
                'stock_movements','warehouse_stock','warehouses','inventory_items','inventory_categories',
                'customer_appliances','appliance_types','appliance_brands',
                'service_city_prices','domain_services','domains','areas','cities']:
        op.drop_table(tbl)
