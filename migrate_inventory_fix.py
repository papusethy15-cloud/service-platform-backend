"""
Run once: python migrate_inventory_fix.py
Fixes column mismatches between SQLAlchemy models and actual PostgreSQL tables.

Missing columns found from error logs:
  inventory_items     : barcode, brand_id, hsn_code, mrp, reserved_stock, reorder_qty, is_consumable, is_serialised, image_url, gst_percent
  inventory_categories: icon, sort_order
  inventory_brands    : entire table missing
  warehouses          : code, city_id, manager_id, phone, is_default
  warehouse_stock     : entire table missing (needed for stock view)
  technician_stock    : entire table missing
  technician_stock_logs: entire table missing
  stock_movements     : from_warehouse_id, to_warehouse_id, technician_id, booking_id, reference_no, batch_no, unit_cost, performed_by
"""
import asyncio
from sqlalchemy import text
from app.core.database import engine

MIGRATIONS = [
    # ── inventory_brands table (completely missing) ─────────────────────────
    ("inventory_brands table", """
        CREATE TABLE IF NOT EXISTS inventory_brands (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name       VARCHAR(100) NOT NULL UNIQUE,
            is_active  BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """),

    # ── inventory_categories: add missing columns ───────────────────────────
    ("inventory_categories.icon column",
        "ALTER TABLE inventory_categories ADD COLUMN IF NOT EXISTS icon VARCHAR(10)"),
    ("inventory_categories.sort_order column",
        "ALTER TABLE inventory_categories ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0"),

    # ── inventory_items: add all missing columns ────────────────────────────
    ("inventory_items.barcode column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS barcode VARCHAR(100) UNIQUE"),
    ("inventory_items.brand_id column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS brand_id UUID REFERENCES inventory_brands(id)"),
    ("inventory_items.image_url column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS image_url VARCHAR(500)"),
    ("inventory_items.hsn_code column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS hsn_code VARCHAR(20)"),
    ("inventory_items.gst_percent column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS gst_percent FLOAT DEFAULT 18.0"),
    ("inventory_items.mrp column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS mrp FLOAT DEFAULT 0"),
    ("inventory_items.reserved_stock column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS reserved_stock INTEGER DEFAULT 0"),
    ("inventory_items.reorder_qty column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS reorder_qty INTEGER DEFAULT 0"),
    ("inventory_items.is_consumable column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS is_consumable BOOLEAN DEFAULT FALSE"),
    ("inventory_items.is_serialised column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS is_serialised BOOLEAN DEFAULT FALSE"),
    ("inventory_items.updated_at column",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE"),

    # ── warehouses: add missing columns ────────────────────────────────────
    ("warehouses.code column",
        "ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS code VARCHAR(20) UNIQUE"),
    ("warehouses.city_id column",
        "ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS city_id UUID"),
    ("warehouses.manager_id column",
        "ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS manager_id UUID REFERENCES users(id)"),
    ("warehouses.phone column",
        "ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS phone VARCHAR(20)"),
    ("warehouses.is_default column",
        "ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT FALSE"),

    # ── warehouse_stock table (may be missing) ──────────────────────────────
    ("warehouse_stock table", """
        CREATE TABLE IF NOT EXISTS warehouse_stock (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            warehouse_id UUID NOT NULL REFERENCES warehouses(id),
            item_id      UUID NOT NULL REFERENCES inventory_items(id),
            quantity     INTEGER DEFAULT 0,
            reserved_qty INTEGER DEFAULT 0,
            updated_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (warehouse_id, item_id)
        )
    """),
    ("ix_wh_stock_item index",
        "CREATE INDEX IF NOT EXISTS ix_wh_stock_item ON warehouse_stock(item_id)"),
    ("ix_wh_stock_wh index",
        "CREATE INDEX IF NOT EXISTS ix_wh_stock_wh ON warehouse_stock(warehouse_id)"),

    # ── technician_stock table ──────────────────────────────────────────────
    ("technician_stock table", """
        CREATE TABLE IF NOT EXISTS technician_stock (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            technician_id  UUID NOT NULL REFERENCES technicians(id),
            item_id        UUID NOT NULL REFERENCES inventory_items(id),
            quantity       INTEGER DEFAULT 0,
            assigned_qty   INTEGER DEFAULT 0,
            consumed_qty   INTEGER DEFAULT 0,
            returned_qty   INTEGER DEFAULT 0,
            updated_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (technician_id, item_id)
        )
    """),

    # ── technician_stock_logs table ─────────────────────────────────────────
    ("technician_stock_logs table", """
        CREATE TABLE IF NOT EXISTS technician_stock_logs (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            technician_id  UUID NOT NULL REFERENCES technicians(id),
            item_id        UUID NOT NULL REFERENCES inventory_items(id),
            booking_id     UUID REFERENCES bookings(id),
            warehouse_id   UUID REFERENCES warehouses(id),
            status         VARCHAR(20) NOT NULL,
            quantity       INTEGER NOT NULL,
            notes          TEXT,
            performed_by   UUID REFERENCES users(id),
            created_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """),

    # ── stock_movements: add missing columns ────────────────────────────────
    ("stock_movements.from_warehouse_id column",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS from_warehouse_id UUID REFERENCES warehouses(id)"),
    ("stock_movements.to_warehouse_id column",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS to_warehouse_id UUID REFERENCES warehouses(id)"),
    ("stock_movements.technician_id column",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS technician_id UUID REFERENCES technicians(id)"),
    ("stock_movements.booking_id column",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS booking_id UUID REFERENCES bookings(id)"),
    ("stock_movements.reference_no column",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS reference_no VARCHAR(100)"),
    ("stock_movements.batch_no column",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS batch_no VARCHAR(100)"),
    ("stock_movements.unit_cost column",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS unit_cost FLOAT"),
    ("stock_movements.performed_by column",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS performed_by UUID REFERENCES users(id)"),

    # ── inventory_reorder_rules table ───────────────────────────────────────
    ("inventory_reorder_rules table", """
        CREATE TABLE IF NOT EXISTS inventory_reorder_rules (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            item_id             UUID NOT NULL UNIQUE REFERENCES inventory_items(id),
            warehouse_id        UUID REFERENCES warehouses(id),
            reorder_level       INTEGER NOT NULL,
            reorder_qty         INTEGER NOT NULL,
            preferred_vendor_id UUID,
            is_active           BOOLEAN DEFAULT TRUE,
            created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """),

    # ── indexes ─────────────────────────────────────────────────────────────
    ("ix_inv_item_brand index",
        "CREATE INDEX IF NOT EXISTS ix_inv_item_brand ON inventory_items(brand_id)"),
    ("ix_inv_item_cat index",
        "CREATE INDEX IF NOT EXISTS ix_inv_item_cat ON inventory_items(category_id)"),
    ("ix_mv_item index",
        "CREATE INDEX IF NOT EXISTS ix_mv_item ON stock_movements(item_id)"),
    ("ix_mv_created index",
        "CREATE INDEX IF NOT EXISTS ix_mv_created ON stock_movements(created_at)"),
]


async def main():
    print("=== Inventory Schema Fix Migration ===\n")
    async with engine.begin() as conn:
        for name, sql in MIGRATIONS:
            print(f"  {name} ...", end=" ", flush=True)
            try:
                await conn.execute(text(sql))
                print("OK")
            except Exception as e:
                short = str(e)[:90]
                print(f"skipped ({short})")
    print("\n✅ Inventory fix migration done.")

asyncio.run(main())
