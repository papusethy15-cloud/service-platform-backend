"""Complete inventory schema — add all missing columns and tables

Revision ID: 009_inventory_complete
Revises: 008_fix_appliance_column_names
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '009_inventory_complete'
down_revision = '008_fix_appliance_column_names'
branch_labels = None
depends_on = None


def col(table, column):
    conn = op.get_bind()
    return conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column}).scalar() > 0


def tbl(name):
    conn = op.get_bind()
    return conn.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=:t"
    ), {"t": name}).scalar() > 0


def add(table, column, col_type, **kw):
    if not col(table, column):
        op.add_column(table, sa.Column(column, col_type, **kw))


def upgrade():
    # ── warehouses ────────────────────────────────────────────────
    add('warehouses', 'code',       sa.String(20),  nullable=True)
    add('warehouses', 'city',       sa.String(100), nullable=True)
    add('warehouses', 'phone',      sa.String(20),  nullable=True)
    add('warehouses', 'is_default', sa.Boolean,     server_default='false')

    # ── warehouse_stock ───────────────────────────────────────────
    add('warehouse_stock', 'reserved_qty', sa.Integer, server_default='0')

    # ── inventory_categories ──────────────────────────────────────
    add('inventory_categories', 'icon',       sa.String(10), nullable=True)
    add('inventory_categories', 'sort_order', sa.Integer,    server_default='0')

    # ── inventory_items ───────────────────────────────────────────
    add('inventory_items', 'barcode',         sa.String(100), nullable=True)
    add('inventory_items', 'brand_id',        postgresql.UUID(as_uuid=True), nullable=True)
    add('inventory_items', 'reserved_stock',  sa.Integer, server_default='0')
    add('inventory_items', 'reorder_qty',     sa.Integer, server_default='0')
    add('inventory_items', 'mrp',             sa.Float,   server_default='0')
    add('inventory_items', 'image_url',       sa.String(500), nullable=True)
    add('inventory_items', 'is_consumable',   sa.Boolean, server_default='false')
    add('inventory_items', 'is_serialised',   sa.Boolean, server_default='false')

    # ── stock_movements — fix column name mismatch ────────────────
    # DB has reference_id, model uses reference_no
    add('stock_movements', 'reference_no',   sa.String(100), nullable=True)
    add('stock_movements', 'batch_no',       sa.String(100), nullable=True)
    add('stock_movements', 'technician_id',  postgresql.UUID(as_uuid=True), nullable=True)
    add('stock_movements', 'booking_id',     postgresql.UUID(as_uuid=True), nullable=True)
    add('stock_movements', 'unit_cost',      sa.Float, nullable=True)

    # ── inventory_brands (new table) ──────────────────────────────
    if not tbl('inventory_brands'):
        op.create_table('inventory_brands',
            sa.Column('id',        postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('name',      sa.String(100), nullable=False),
            sa.Column('is_active', sa.Boolean, server_default='true'),
            sa.Column('created_at',sa.DateTime(timezone=True), server_default=sa.text('now()')),
        )

    # ── technician_stock (new table) ─────────────────────────────
    if not tbl('technician_stock'):
        op.create_table('technician_stock',
            sa.Column('id',             postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('technician_id',  postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('technicians.id'), nullable=False),
            sa.Column('item_id',        postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id'), nullable=False),
            sa.Column('quantity',       sa.Integer, server_default='0'),
            sa.Column('assigned_qty',   sa.Integer, server_default='0'),
            sa.Column('consumed_qty',   sa.Integer, server_default='0'),
            sa.Column('returned_qty',   sa.Integer, server_default='0'),
            sa.Column('is_active',      sa.Boolean, server_default='true'),
            sa.Column('created_at',     sa.DateTime(timezone=True), server_default=sa.text('now()')),
            sa.Column('updated_at',     sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('technician_id', 'item_id', name='uq_tech_stock'),
        )

    # ── technician_stock_logs (new table) ─────────────────────────
    if not tbl('technician_stock_logs'):
        op.create_table('technician_stock_logs',
            sa.Column('id',            postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('technician_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('technicians.id'), nullable=False),
            sa.Column('item_id',       postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id'), nullable=False),
            sa.Column('warehouse_id',  postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('booking_id',    postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('status',        sa.String(30), server_default='ASSIGNED'),
            sa.Column('quantity',      sa.Integer, nullable=False),
            sa.Column('notes',         sa.Text, nullable=True),
            sa.Column('performed_by',  postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('is_active',     sa.Boolean, server_default='true'),
            sa.Column('created_at',    sa.DateTime(timezone=True), server_default=sa.text('now()')),
        )

    # ── reorder_rules (new table) ─────────────────────────────────
    if not tbl('reorder_rules'):
        op.create_table('reorder_rules',
            sa.Column('id',                  postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('item_id',             postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id'), nullable=False),
            sa.Column('warehouse_id',        postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('preferred_vendor_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('reorder_level',       sa.Integer, nullable=False),
            sa.Column('reorder_qty',         sa.Integer, nullable=False),
            sa.Column('is_active',           sa.Boolean, server_default='true'),
            sa.Column('created_at',          sa.DateTime(timezone=True), server_default=sa.text('now()')),
        )

    # ── stock_challan (transfer challans) ─────────────────────────
    if not tbl('stock_challans'):
        op.create_table('stock_challans',
            sa.Column('id',               postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('challan_number',   sa.String(50), unique=True, nullable=False),
            sa.Column('challan_type',     sa.String(30), nullable=False),  # TRANSFER|ASSIGN|RETURN|SALE
            sa.Column('from_warehouse_id',postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('to_warehouse_id',  postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('technician_id',    postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('booking_id',       postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('customer_id',      postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('status',           sa.String(20), server_default='DRAFT'),
            sa.Column('notes',            sa.Text, nullable=True),
            sa.Column('total_items',      sa.Integer, server_default='0'),
            sa.Column('total_value',      sa.Float,   server_default='0'),
            sa.Column('created_by',       postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('approved_by',      postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('dispatched_at',    sa.DateTime(timezone=True), nullable=True),
            sa.Column('received_at',      sa.DateTime(timezone=True), nullable=True),
            sa.Column('is_active',        sa.Boolean, server_default='true'),
            sa.Column('created_at',       sa.DateTime(timezone=True), server_default=sa.text('now()')),
            sa.Column('updated_at',       sa.DateTime(timezone=True), nullable=True),
        )

    # ── stock_challan_items ───────────────────────────────────────
    if not tbl('stock_challan_items'):
        op.create_table('stock_challan_items',
            sa.Column('id',           postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('challan_id',   postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('stock_challans.id'), nullable=False),
            sa.Column('item_id',      postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id'), nullable=False),
            sa.Column('quantity',     sa.Integer, nullable=False),
            sa.Column('unit_cost',    sa.Float,   nullable=True),
            sa.Column('selling_price',sa.Float,   nullable=True),
            sa.Column('total_value',  sa.Float,   nullable=True),
            sa.Column('notes',        sa.Text,    nullable=True),
            sa.Column('is_active',    sa.Boolean, server_default='true'),
            sa.Column('created_at',   sa.DateTime(timezone=True), server_default=sa.text('now()')),
        )

    # ── stock_sales (direct spare part sale) ─────────────────────
    if not tbl('stock_sales'):
        op.create_table('stock_sales',
            sa.Column('id',           postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('sale_number',  sa.String(50), unique=True, nullable=False),
            sa.Column('customer_id',  postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('warehouse_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('challan_id',   postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('subtotal',     sa.Float, server_default='0'),
            sa.Column('gst_amount',   sa.Float, server_default='0'),
            sa.Column('total',        sa.Float, server_default='0'),
            sa.Column('payment_mode', sa.String(30), nullable=True),
            sa.Column('notes',        sa.Text,  nullable=True),
            sa.Column('sold_by',      postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('is_active',    sa.Boolean, server_default='true'),
            sa.Column('created_at',   sa.DateTime(timezone=True), server_default=sa.text('now()')),
        )

    if not tbl('stock_sale_items'):
        op.create_table('stock_sale_items',
            sa.Column('id',           postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('sale_id',      postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('stock_sales.id'), nullable=False),
            sa.Column('item_id',      postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id'), nullable=False),
            sa.Column('quantity',     sa.Integer, nullable=False),
            sa.Column('unit_price',   sa.Float,   nullable=False),
            sa.Column('gst_percent',  sa.Float,   server_default='18'),
            sa.Column('total',        sa.Float,   nullable=False),
            sa.Column('is_active',    sa.Boolean, server_default='true'),
            sa.Column('created_at',   sa.DateTime(timezone=True), server_default=sa.text('now()')),
        )


def downgrade():
    for t in ['stock_sale_items','stock_sales','stock_challan_items',
              'stock_challans','reorder_rules','technician_stock_logs',
              'technician_stock','inventory_brands']:
        try:
            op.drop_table(t)
        except Exception:
            pass
