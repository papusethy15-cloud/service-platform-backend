"""Fix all remaining model-vs-DB column mismatches. Run once."""
import asyncio, sys
sys.path.insert(0, r'C:\MyWorkspace\Palei Solutions\backend')
from sqlalchemy import text
from app.core.database import engine

MIGRATIONS = [
    # warehouse_stock missing reserved_qty
    ("warehouse_stock.reserved_qty",
        "ALTER TABLE warehouse_stock ADD COLUMN IF NOT EXISTS reserved_qty INTEGER DEFAULT 0"),

    # audit_logs missing columns
    ("audit_logs.description",
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS description TEXT"),
    ("audit_logs.user_name",
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS user_name VARCHAR(200)"),
    ("audit_logs.user_role",
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS user_role VARCHAR(50)"),

    # leave_requests missing reviewed_at
    ("leave_requests.reviewed_at",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP WITH TIME ZONE"),

    # notification_templates missing title
    ("notification_templates.title",
        "ALTER TABLE notification_templates ADD COLUMN IF NOT EXISTS title VARCHAR(200)"),

    # Missing tables
    ("technician_availability table", """
        CREATE TABLE IF NOT EXISTS technician_availability (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            technician_id UUID NOT NULL REFERENCES technicians(id),
            day_of_week   INTEGER NOT NULL,
            start_time    TIME NOT NULL,
            end_time      TIME NOT NULL,
            is_available  BOOLEAN DEFAULT TRUE,
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """),

    ("booking_part_usage table", """
        CREATE TABLE IF NOT EXISTS booking_part_usage (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            booking_id    UUID NOT NULL REFERENCES bookings(id),
            item_id       UUID NOT NULL REFERENCES inventory_items(id),
            technician_id UUID REFERENCES technicians(id),
            warehouse_id  UUID REFERENCES warehouses(id),
            quantity      INTEGER NOT NULL,
            unit_cost     FLOAT DEFAULT 0,
            unit_price    FLOAT DEFAULT 0,
            total_amount  FLOAT DEFAULT 0,
            notes         TEXT,
            created_by    UUID REFERENCES users(id),
            is_active     BOOLEAN DEFAULT TRUE,
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """),

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

    # challan_id on stock_movements (was skipped before)
    ("stock_movements.challan_id",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS challan_id UUID REFERENCES transfer_challans(id)"),
]

async def main():
    print("=== Fix All Remaining Schema Issues ===\n")
    async with engine.begin() as conn:
        for name, sql in MIGRATIONS:
            print(f"  {name} ...", end=" ", flush=True)
            try:
                await conn.execute(text(sql))
                print("OK")
            except Exception as e:
                print(f"skipped ({str(e)[:70]})")
    await engine.dispose()
    print("\nAll done. Re-run audit_columns.py to verify.")

asyncio.run(main())
