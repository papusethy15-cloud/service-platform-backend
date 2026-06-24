"""
Domain-aware schema migration.
Run once: python migrate_domain.py

Changes:
  1. Create domain_categories table
  2. Add domain_id column to bookings
  3. Add domain_id column to quotations
  4. Add domain_id column to invoices
  5. Add unique constraint to domain_services (if missing)
"""
import asyncio
from sqlalchemy import text
from app.core.database import engine

MIGRATIONS = [
    # 1. domain_categories table
    (
        "domain_categories table",
        """
        CREATE TABLE IF NOT EXISTS domain_categories (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id   UUID NOT NULL REFERENCES domains(id),
            category_id UUID NOT NULL REFERENCES service_categories(id),
            sort_order  INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT NOW(),
            updated_at  TIMESTAMP DEFAULT NOW(),
            is_active   BOOLEAN DEFAULT TRUE,
            CONSTRAINT uq_domain_category UNIQUE (domain_id, category_id)
        )
        """
    ),
    # 2. bookings.domain_id
    (
        "bookings.domain_id",
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='bookings' AND column_name='domain_id'
            ) THEN
                ALTER TABLE bookings ADD COLUMN domain_id UUID REFERENCES domains(id);
            END IF;
        END $$
        """
    ),
    # 3. quotations.domain_id
    (
        "quotations.domain_id",
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='quotations' AND column_name='domain_id'
            ) THEN
                ALTER TABLE quotations ADD COLUMN domain_id UUID REFERENCES domains(id);
            END IF;
        END $$
        """
    ),
    # 4. invoices.domain_id
    (
        "invoices.domain_id",
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='invoices' AND column_name='domain_id'
            ) THEN
                ALTER TABLE invoices ADD COLUMN domain_id UUID REFERENCES domains(id);
            END IF;
        END $$
        """
    ),
    # 5. domain_services unique constraint
    (
        "domain_services unique constraint",
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name='uq_domain_service' AND table_name='domain_services'
            ) THEN
                ALTER TABLE domain_services ADD CONSTRAINT uq_domain_service UNIQUE (domain_id, service_id);
            END IF;
        END $$
        """
    ),
]

async def main():
    async with engine.begin() as conn:
        for name, sql in MIGRATIONS:
            print(f"Running: {name} ...", end=" ")
            try:
                await conn.execute(text(sql))
                print("OK")
            except Exception as e:
                print(f"SKIPPED ({e})")
    print("\nMigration complete.")

asyncio.run(main())
