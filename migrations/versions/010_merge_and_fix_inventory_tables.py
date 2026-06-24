"""Merge branches + fix missing inventory tables (transfer_challans, direct_sales, booking_part_usage)

Revision ID: 010_inventory_fix
Revises: 009_inventory_complete, 007_fix_inv_brand_fk
Create Date: 2026-06-01

Merges the two parallel migration branches:
  Branch A: 005 → 006_fix_appliance → 007_add_brand → 008_fix_appliance → 009_inventory_complete
  Branch B: 005 → 006_inventory_multi_cat → 007_fix_inv_brand_fk

Then creates the 3 tables the inventory route needs but which don't exist in DB:
  - transfer_challans  (was stock_challans with wrong name)
  - direct_sales       (was stock_sales with wrong name)
  - booking_part_usage (completely missing)
  - inventory_reorder_rules (model uses this name, migrations created reorder_rules)
  - item_service_categories (created by 006_inventory_multi_cat — idempotent check)

Also fixes FK: inventory_items.brand_id → appliance_brands (if not already done).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '010_inventory_fix'
down_revision = ('009_inventory_complete', '007_fix_inv_brand_fk')  # merge point
branch_labels = None
depends_on = None


def _exists(bind, table):
    return bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        f"WHERE table_schema='public' AND table_name='{table}')"
    )).scalar()


def _col_exists(bind, table, col):
    return bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        f"WHERE table_schema='public' AND table_name='{table}' AND column_name='{col}')"
    )).scalar()


def _fk_target(bind, table, column):
    row = bind.execute(sa.text("""
        SELECT ccu.table_name
        FROM information_schema.key_column_usage kcu
        JOIN information_schema.referential_constraints rc
          ON rc.constraint_name = kcu.constraint_name
         AND rc.constraint_schema = kcu.constraint_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = rc.unique_constraint_name
         AND ccu.constraint_schema = rc.constraint_schema
        WHERE kcu.table_schema = 'public'
          AND kcu.table_name   = :tbl
          AND kcu.column_name  = :col
        LIMIT 1
    """), {"tbl": table, "col": column}).fetchone()
    return row[0] if row else None


def upgrade():
    bind = op.get_bind()

    # ── 1. Fix brand FK: inventory_items.brand_id → appliance_brands ──────
    current_target = _fk_target(bind, 'inventory_items', 'brand_id')
    if current_target and current_target != 'appliance_brands':
        # Find constraint name dynamically
        rows = bind.execute(sa.text("""
            SELECT rc.constraint_name
            FROM information_schema.key_column_usage kcu
            JOIN information_schema.referential_constraints rc
              ON rc.constraint_name = kcu.constraint_name
             AND rc.constraint_schema = kcu.constraint_schema
            WHERE kcu.table_schema = 'public'
              AND kcu.table_name   = 'inventory_items'
              AND kcu.column_name  = 'brand_id'
        """)).fetchall()
        for row in rows:
            op.drop_constraint(row[0], 'inventory_items', type_='foreignkey')
        op.create_foreign_key(
            'fk_inv_items_brand_appliance',
            'inventory_items', 'appliance_brands',
            ['brand_id'], ['id'], ondelete='SET NULL'
        )
        print("  Fixed: inventory_items.brand_id → appliance_brands")
    elif not current_target:
        # No FK at all — add it
        op.create_foreign_key(
            'fk_inv_items_brand_appliance',
            'inventory_items', 'appliance_brands',
            ['brand_id'], ['id'], ondelete='SET NULL'
        )
        print("  Added: inventory_items.brand_id → appliance_brands")

    # ── 2. item_service_categories (M2M junction) ──────────────────────────
    if not _exists(bind, 'item_service_categories'):
        op.create_table(
            'item_service_categories',
            sa.Column('item_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id', ondelete='CASCADE'), nullable=False),
            sa.Column('category_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('service_categories.id', ondelete='CASCADE'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True),
                      server_default=sa.text('NOW()')),
            sa.PrimaryKeyConstraint('item_id', 'category_id',
                                    name='pk_item_service_categories'),
        )
        op.create_index('ix_isc_item_id',     'item_service_categories', ['item_id'])
        op.create_index('ix_isc_category_id', 'item_service_categories', ['category_id'])
        print("  Created: item_service_categories")

        # Migrate existing single category_id data
        if _col_exists(bind, 'inventory_items', 'category_id'):
            bind.execute(sa.text("""
                INSERT INTO item_service_categories (item_id, category_id, created_at)
                SELECT i.id, i.category_id, NOW()
                FROM inventory_items i
                WHERE i.category_id IS NOT NULL AND i.is_active = true
                ON CONFLICT DO NOTHING
            """))
            print("  Migrated existing single-category data")

    # ── 3. inventory_reorder_rules (model expects this name) ──────────────
    if not _exists(bind, 'inventory_reorder_rules') and _exists(bind, 'reorder_rules'):
        # Just rename reorder_rules → inventory_reorder_rules
        op.rename_table('reorder_rules', 'inventory_reorder_rules')
        print("  Renamed: reorder_rules → inventory_reorder_rules")
    elif not _exists(bind, 'inventory_reorder_rules'):
        op.create_table(
            'inventory_reorder_rules',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('item_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id'), nullable=False),
            sa.Column('warehouse_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('warehouses.id'), nullable=True),
            sa.Column('reorder_level', sa.Integer(), default=5),
            sa.Column('reorder_qty',   sa.Integer(), default=10),
            sa.Column('preferred_vendor_id', postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('is_active',  sa.Boolean(), server_default='true'),
            sa.Column('created_at', sa.DateTime(timezone=True),
                      server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime(timezone=True),
                      server_default=sa.text('NOW()')),
        )
        print("  Created: inventory_reorder_rules")

    # ── 4. transfer_challans ───────────────────────────────────────────────
    if not _exists(bind, 'transfer_challans'):
        op.create_table(
            'transfer_challans',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('challan_no',        sa.String(30),  nullable=False, unique=True),
            sa.Column('from_warehouse_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('warehouses.id'), nullable=True),
            sa.Column('to_warehouse_id',   postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('warehouses.id'), nullable=True),
            sa.Column('to_technician_id',  postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('technicians.id'), nullable=True),
            sa.Column('items_json',   sa.Text(),    server_default="'[]'"),
            sa.Column('total_qty',    sa.Integer(), server_default='0'),
            sa.Column('total_value',  sa.Float(),   server_default='0'),
            sa.Column('status',       sa.String(20), server_default="'PENDING'"),
            sa.Column('notes',        sa.Text(),    nullable=True),
            sa.Column('reference_no', sa.String(100), nullable=True),
            sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('received_at',   sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_by',  postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('users.id'), nullable=True),
            sa.Column('received_by', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('users.id'), nullable=True),
            sa.Column('is_active',   sa.Boolean(), server_default='true'),
            sa.Column('created_at',  sa.DateTime(timezone=True),
                      server_default=sa.text('NOW()')),
        )
        op.create_index('ix_transfer_challans_no', 'transfer_challans', ['challan_no'])
        print("  Created: transfer_challans")

    # ── 5. direct_sales ───────────────────────────────────────────────────
    if not _exists(bind, 'direct_sales'):
        op.create_table(
            'direct_sales',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('sale_no',         sa.String(30),  nullable=False, unique=True),
            sa.Column('warehouse_id',    postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('warehouses.id'), nullable=False),
            sa.Column('customer_id',     postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('customers.id'), nullable=True),
            sa.Column('customer_name',   sa.String(200), nullable=True),
            sa.Column('customer_mobile', sa.String(20),  nullable=True),
            sa.Column('booking_id',      postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('bookings.id'), nullable=True),
            sa.Column('items_json',      sa.Text(),   server_default="'[]'"),
            sa.Column('subtotal',        sa.Float(),  server_default='0'),
            sa.Column('gst_amount',      sa.Float(),  server_default='0'),
            sa.Column('total_amount',    sa.Float(),  server_default='0'),
            sa.Column('payment_method',  sa.String(30), server_default="'CASH'"),
            sa.Column('payment_status',  sa.String(20), server_default="'PAID'"),
            sa.Column('notes',   sa.Text(), nullable=True),
            sa.Column('sold_by', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('users.id'), nullable=True),
            sa.Column('is_active',  sa.Boolean(), server_default='true'),
            sa.Column('created_at', sa.DateTime(timezone=True),
                      server_default=sa.text('NOW()')),
        )
        op.create_index('ix_direct_sales_no', 'direct_sales', ['sale_no'])
        print("  Created: direct_sales")

    # ── 6. booking_part_usage ─────────────────────────────────────────────
    if not _exists(bind, 'booking_part_usage'):
        op.create_table(
            'booking_part_usage',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('booking_id',    postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('bookings.id'), nullable=False),
            sa.Column('item_id',       postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id'), nullable=False),
            sa.Column('technician_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('technicians.id'), nullable=True),
            sa.Column('warehouse_id',  postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('warehouses.id'), nullable=True),
            sa.Column('quantity',      sa.Integer(), nullable=False),
            sa.Column('unit_cost',     sa.Float(),   server_default='0'),
            sa.Column('unit_price',    sa.Float(),   server_default='0'),
            sa.Column('total_amount',  sa.Float(),   server_default='0'),
            sa.Column('notes',         sa.Text(),    nullable=True),
            sa.Column('created_by',    postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('users.id'), nullable=True),
            sa.Column('is_active',  sa.Boolean(), server_default='true'),
            sa.Column('created_at', sa.DateTime(timezone=True),
                      server_default=sa.text('NOW()')),
        )
        op.create_index('ix_booking_part_booking_id', 'booking_part_usage', ['booking_id'])
        op.create_index('ix_booking_part_item_id',    'booking_part_usage', ['item_id'])
        print("  Created: booking_part_usage")

    # ── 7. quotation_part_items: add inventory_item_id if missing ─────────
    if not _col_exists(bind, 'quotation_part_items', 'inventory_item_id'):
        op.add_column('quotation_part_items',
            sa.Column('inventory_item_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id'), nullable=True))
        print("  Added: quotation_part_items.inventory_item_id")

    if not _col_exists(bind, 'quotation_part_items', 'is_from_stock'):
        op.add_column('quotation_part_items',
            sa.Column('is_from_stock', sa.Boolean(), server_default='false'))
        print("  Added: quotation_part_items.is_from_stock")


def downgrade():
    op.drop_table('booking_part_usage')
    op.drop_table('direct_sales')
    op.drop_table('transfer_challans')
    op.drop_table('item_service_categories')
