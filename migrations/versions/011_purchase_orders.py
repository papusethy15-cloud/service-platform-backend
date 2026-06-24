"""Add purchase_orders table for advanced inventory purchasing

Revision ID: 011_purchase_orders
Revises: 010_merge_and_fix_inventory_tables
Create Date: 2026-06-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '011_purchase_orders'
down_revision = '010_inventory_fix'
branch_labels = None
depends_on = None


def _exists(bind, table):
    res = bind.execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name=:t)"
    ), {"t": table})
    return res.scalar()


def _col_exists(bind, table, col):
    res = bind.execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name=:t AND column_name=:c)"
    ), {"t": table, "c": col})
    return res.scalar()


def upgrade():
    bind = op.get_bind()

    # ── purchase_orders ───────────────────────────────────────────────────
    if not _exists(bind, 'purchase_orders'):
        op.create_table(
            'purchase_orders',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('po_number',    sa.String(30),  nullable=False, unique=True),
            sa.Column('vendor_id',    postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('vendors.id'), nullable=True),
            sa.Column('vendor_name',  sa.String(200), nullable=True),   # denorm for walk-in vendors
            sa.Column('vendor_invoice_no', sa.String(100), nullable=True),
            sa.Column('warehouse_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('warehouses.id'), nullable=False),
            # items: JSON array [{item_id, item_name, sku, unit, qty, unit_cost, total_cost}]
            sa.Column('items_json',   sa.Text(), nullable=False, server_default="'[]'"),
            sa.Column('subtotal',     sa.Float(), server_default='0'),
            sa.Column('tax_amount',   sa.Float(), server_default='0'),
            sa.Column('total_amount', sa.Float(), server_default='0'),
            sa.Column('payment_method', sa.String(30), server_default="'CASH'"),
            sa.Column('payment_status',  sa.String(20), server_default="'PAID'"),
            # status: DRAFT → ORDERED → RECEIVED → CANCELLED
            sa.Column('status',       sa.String(20), server_default="'RECEIVED'"),
            sa.Column('notes',        sa.Text(), nullable=True),
            sa.Column('received_at',  sa.DateTime(timezone=True), nullable=True,
                      server_default=sa.text('NOW()')),
            sa.Column('created_by',   postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('users.id'), nullable=True),
            sa.Column('is_active',    sa.Boolean(), server_default='true'),
            sa.Column('created_at',   sa.DateTime(timezone=True),
                      server_default=sa.text('NOW()')),
        )
        op.create_index('ix_po_number',    'purchase_orders', ['po_number'])
        op.create_index('ix_po_warehouse', 'purchase_orders', ['warehouse_id'])
        op.create_index('ix_po_vendor',    'purchase_orders', ['vendor_id'])
        print("  Created: purchase_orders")

    # ── stock_movements: add po_id column if missing ──────────────────────
    if not _col_exists(bind, 'stock_movements', 'po_id'):
        op.add_column('stock_movements',
            sa.Column('po_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('purchase_orders.id'), nullable=True))
        print("  Added: stock_movements.po_id")

    # ── stock_movements: add from_warehouse_id / to_warehouse_id if missing
    for col in ['from_warehouse_id', 'to_warehouse_id']:
        if not _col_exists(bind, 'stock_movements', col):
            op.add_column('stock_movements',
                sa.Column(col, postgresql.UUID(as_uuid=True),
                          sa.ForeignKey('warehouses.id'), nullable=True))
            print(f"  Added: stock_movements.{col}")


def downgrade():
    op.drop_table('purchase_orders')
