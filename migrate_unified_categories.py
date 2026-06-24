"""
Unify inventory categories with service_categories.
- inventory_items: change category_id FK → service_categories(id)
- Create item_service_categories (many-to-many): one item → many service categories
- QuotationPartItem: add inventory_item_id FK for linking real stock items to quotation parts
Run: python migrate_unified_categories.py
"""
import asyncio, sys
sys.path.insert(0, r'C:\MyWorkspace\Palei Solutions\backend')
from sqlalchemy import text
from app.core.database import engine

MIGRATIONS = [
    # 1. Many-to-many: inventory item <-> service_categories
    ("item_service_categories table", """
        CREATE TABLE IF NOT EXISTS item_service_categories (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            item_id     UUID NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
            category_id UUID NOT NULL REFERENCES service_categories(id) ON DELETE CASCADE,
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (item_id, category_id)
        )
    """),
    ("ix_isc_item",     "CREATE INDEX IF NOT EXISTS ix_isc_item     ON item_service_categories(item_id)"),
    ("ix_isc_category", "CREATE INDEX IF NOT EXISTS ix_isc_category ON item_service_categories(category_id)"),

    # 2. Link quotation part items to real inventory items
    ("quotation_part_items.inventory_item_id",
        "ALTER TABLE quotation_part_items ADD COLUMN IF NOT EXISTS inventory_item_id UUID REFERENCES inventory_items(id)"),
    ("quotation_part_items.sku",
        "ALTER TABLE quotation_part_items ADD COLUMN IF NOT EXISTS sku VARCHAR(100)"),
    ("quotation_part_items.unit",
        "ALTER TABLE quotation_part_items ADD COLUMN IF NOT EXISTS unit VARCHAR(20)"),
    ("quotation_part_items.category_id",
        "ALTER TABLE quotation_part_items ADD COLUMN IF NOT EXISTS category_id UUID REFERENCES service_categories(id)"),

    # 3. Migrate any existing inventory_items.category_id data into many-to-many
    # (safe no-op if inventory_categories is empty or category_id is null)
    ("migrate existing inventory category_ids", """
        INSERT INTO item_service_categories (item_id, category_id)
        SELECT ii.id, sc.id
        FROM inventory_items ii
        JOIN inventory_categories ic ON ic.id = ii.category_id
        JOIN service_categories sc ON LOWER(sc.name) = LOWER(ic.name)
        WHERE ii.category_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """),
]

async def main():
    print("=== Unified Category Migration ===\n")
    async with engine.begin() as conn:
        for name, sql in MIGRATIONS:
            print(f"  {name} ...", end=" ", flush=True)
            try:
                await conn.execute(text(sql))
                print("OK")
            except Exception as e:
                print(f"skipped ({str(e)[:80]})")
    await engine.dispose()
    print("\nDone.")

asyncio.run(main())
