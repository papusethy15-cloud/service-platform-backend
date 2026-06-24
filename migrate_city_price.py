"""
Run once to fix service_city_prices.price column type: String → Float
and add the unique constraint.
Usage:  python migrate_city_price.py
"""
import asyncio
from sqlalchemy import text
from app.core.database import engine

async def main():
    async with engine.begin() as conn:
        # Check current type
        result = await conn.execute(text("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name='service_city_prices' AND column_name='price'
        """))
        row = result.fetchone()
        print(f"Current price column type: {row[0] if row else 'not found'}")

        if row and row[0] != 'double precision':
            print("Altering price column from VARCHAR → FLOAT ...")
            await conn.execute(text("""
                ALTER TABLE service_city_prices
                ALTER COLUMN price TYPE FLOAT USING price::FLOAT
            """))
            print("Done — price column is now FLOAT.")
        else:
            print("Price column is already FLOAT — no migration needed.")

        # Add unique constraint if not already there
        uc_exists = await conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.table_constraints
            WHERE constraint_name='uq_service_city_price'
            AND table_name='service_city_prices'
        """))
        if uc_exists.scalar() == 0:
            try:
                await conn.execute(text("""
                    ALTER TABLE service_city_prices
                    ADD CONSTRAINT uq_service_city_price UNIQUE (service_id, city_id)
                """))
                print("Unique constraint added.")
            except Exception as e:
                print(f"Unique constraint skipped (may already exist): {e}")
        else:
            print("Unique constraint already exists.")

asyncio.run(main())
