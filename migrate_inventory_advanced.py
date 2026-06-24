"""Run once: python migrate_inventory_advanced.py"""
import asyncio
from sqlalchemy import text
from app.core.database import engine

MIGRATIONS = [
    ("transfer_challans table", """
        CREATE TABLE IF NOT EXISTS transfer_challans (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            challan_no        VARCHAR(30) NOT NULL UNIQUE,
            from_warehouse_id UUID REFERENCES warehouses(id),
            to_warehouse_id   UUID REFERENCES warehouses(id),
            to_technician_id  UUID REFERENCES technicians(id),
            items_json        TEXT NOT NULL DEFAULT '[]',
            total_qty         INTEGER DEFAULT 0,
            total_value       FLOAT DEFAULT 0,
            status            VARCHAR(20) DEFAULT 'PENDING',
            notes             TEXT,
            reference_no      VARCHAR(100),
            dispatched_at     TIMESTAMP WITH TIME ZONE,
            received_at       TIMESTAMP WITH TIME ZONE,
            created_by        UUID REFERENCES users(id),
            received_by       UUID REFERENCES users(id),
            is_active         BOOLEAN DEFAULT TRUE,
            created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """),
    ("direct_sales table", """
        CREATE TABLE IF NOT EXISTS direct_sales (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            sale_no          VARCHAR(30) NOT NULL UNIQUE,
            warehouse_id     UUID NOT NULL REFERENCES warehouses(id),
            customer_id      UUID REFERENCES customers(id),
            customer_name    VARCHAR(200),
            customer_mobile  VARCHAR(20),
            booking_id       UUID REFERENCES bookings(id),
            items_json       TEXT NOT NULL DEFAULT '[]',
            subtotal         FLOAT DEFAULT 0,
            gst_amount       FLOAT DEFAULT 0,
            total_amount     FLOAT DEFAULT 0,
            payment_method   VARCHAR(30) DEFAULT 'CASH',
            payment_status   VARCHAR(20) DEFAULT 'PAID',
            notes            TEXT,
            sold_by          UUID REFERENCES users(id),
            is_active        BOOLEAN DEFAULT TRUE,
            created_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """),
    ("booking_part_usage table", """
        CREATE TABLE IF NOT EXISTS booking_part_usage (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            booking_id      UUID NOT NULL REFERENCES bookings(id),
            item_id         UUID NOT NULL REFERENCES inventory_items(id),
            technician_id   UUID REFERENCES technicians(id),
            warehouse_id    UUID REFERENCES warehouses(id),
            quantity        INTEGER NOT NULL,
            unit_cost       FLOAT DEFAULT 0,
            unit_price      FLOAT DEFAULT 0,
            total_amount    FLOAT DEFAULT 0,
            notes           TEXT,
            created_by      UUID REFERENCES users(id),
            is_active       BOOLEAN DEFAULT TRUE,
            created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """),
    ("inventory_items.brand_id index", "CREATE INDEX IF NOT EXISTS ix_inv_item_brand ON inventory_items(brand_id)"),
    ("inventory_items.category_id index", "CREATE INDEX IF NOT EXISTS ix_inv_item_cat ON inventory_items(category_id)"),
    ("stock_movements challan_id", """
        ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS challan_id UUID REFERENCES transfer_challans(id)
    """),
]

async def main():
    async with engine.begin() as conn:
        for name, sql in MIGRATIONS:
            print(f"  {name} ...", end=" ", flush=True)
            try:
                await conn.execute(text(sql)); print("OK")
            except Exception as e:
                print(f"skipped ({str(e)[:70]})")
    print("\nInventory advanced migration done.")

asyncio.run(main())
