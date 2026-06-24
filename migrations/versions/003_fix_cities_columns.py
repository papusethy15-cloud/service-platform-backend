"""fix all column mismatches between models and DB

Revision ID: 003_fix_cities
Revises: 002_all_missing
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '003_fix_cities'
down_revision = '002_all_missing'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    def col(table, column):
        return bind.execute(sa.text(
            f"SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{table}' AND column_name='{column}')"
        )).scalar()

    def add(table, column, col_type, **kw):
        if not col(table, column):
            op.add_column(table, sa.Column(column, col_type, **kw))

    # ── CITIES ───────────────────────────────────────────────
    add('cities', 'country',             sa.String(100),  server_default='India')
    add('cities', 'base_travel_charge',  sa.Float,        server_default='0.0')
    add('cities', 'surge_multiplier',    sa.Float,        server_default='1.0')
    add('cities', 'sort_order',          sa.Integer,      server_default='0')

    # ── AREAS ────────────────────────────────────────────────
    add('areas', 'pincode', sa.String(20), nullable=True)

    # ── WALLETS — model uses user_id but table has technician_id
    # The wallet model (wallet.py) uses user_id, but we created technician_id in 002
    # Add user_id as alias; keep both for compatibility
    add('wallets', 'user_id', postgresql.UUID(as_uuid=True), nullable=True)

    # ── WALLET_TRANSACTIONS — model has extra columns ─────────
    add('wallet_transactions', 'description',  sa.Text,       nullable=True)
    add('wallet_transactions', 'reference_id', sa.String(200),nullable=True)
    # balance_before already in table; balance_after needs check
    add('wallet_transactions', 'balance_after', sa.Float, server_default='0.0')

    # ── COMMISSION_RULES — model has role, commission_type, rate, applies_to ─
    add('commission_rules', 'role',            sa.String(50),  nullable=True)
    add('commission_rules', 'commission_type', sa.String(20),  nullable=True)
    add('commission_rules', 'rate',            sa.Float,       server_default='0.0')
    add('commission_rules', 'applies_to',      sa.String(50),  nullable=True)

    # ── COMMISSIONS — model has base_amount, payout_date ─────
    add('commissions', 'base_amount',  sa.Float,       server_default='0.0')
    add('commissions', 'payout_date',  sa.DateTime,    nullable=True)

    # ── COUPONS — model has description, discount_type, discount_value
    # Our table has: code, name, coupon_type, value — model uses discount_type/discount_value
    add('coupons', 'description',   sa.Text,    nullable=True)
    add('coupons', 'discount_type', sa.String(20), nullable=True)
    add('coupons', 'discount_value',sa.Float,   nullable=True)

    # ── COUPON_USAGES — model uses user_id & discount_applied ─
    add('coupon_usages', 'user_id',          postgresql.UUID(as_uuid=True), nullable=True)
    add('coupon_usages', 'discount_applied', sa.Float,  nullable=True)
    add('coupon_usages', 'used_at',          sa.DateTime, nullable=True)

    # ── ATTENDANCE — model has check_in_lat, check_in_lng, approved_by ──
    add('attendance', 'check_in_lat', sa.Float,   nullable=True)
    add('attendance', 'check_in_lng', sa.Float,   nullable=True)
    add('attendance', 'approved_by',  postgresql.UUID(as_uuid=True), nullable=True)

    # ── LEAVE_REQUESTS — model has approved_by ───────────────
    add('leave_requests', 'approved_by', postgresql.UUID(as_uuid=True), nullable=True)

    # ── INVENTORY_ITEMS — model may have description, quantity ─
    add('inventory_items', 'description',  sa.Text,    nullable=True)
    add('inventory_items', 'quantity',     sa.Integer, server_default='0')

    # ── WAREHOUSES — model may have manager_id ───────────────
    add('warehouses', 'manager_id', postgresql.UUID(as_uuid=True), nullable=True)

    # ── TECHNICIAN_SKILLS — extra columns in model ───────────
    add('technician_skills', 'proficiency', sa.String(20), server_default='INTERMEDIATE')

    # ── ESCALATIONS — model may have booking_id as FK ────────
    # already correct in 002

    # ── NOTIFICATIONS — model may have channel field ──────────
    add('notifications', 'channel',       sa.String(20), server_default='IN_APP')
    add('notifications', 'sent_at',       sa.DateTime,   nullable=True)
    add('notifications', 'reference_type',sa.String(50), nullable=True)

    # ── KNOWLEDGE_BASE_ARTICLES — extra fields ────────────────
    add('knowledge_base_articles', 'article_type', sa.String(30), server_default='TEXT')
    add('knowledge_base_articles', 'file_url',     sa.String(500), nullable=True)
    add('knowledge_base_articles', 'views',        sa.Integer,    server_default='0')


def downgrade():
    pass  # Column drops are destructive — not auto-reverting
