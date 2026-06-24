"""
Compares SQLAlchemy model columns vs actual DB columns for every mapped table.
Prints any column present in model but missing from DB.
"""
import asyncio, sys
sys.path.insert(0, r'C:\MyWorkspace\Palei Solutions\backend')

from sqlalchemy import text, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from app.core.database import engine
from app.models.base import Base

# Import all models so they register with Base
import app.models.user
import app.models.booking
import app.models.customer
import app.models.technician
import app.models.service
import app.models.invoice
import app.models.payment
import app.models.inventory
import app.models.city

async def main():
    async with engine.connect() as conn:
        missing_total = 0
        for mapper in Base.registry.mappers:
            tbl = mapper.local_table
            tbl_name = tbl.name
            model_cols = {c.key for c in tbl.columns}

            r = await conn.execute(text(
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_name='{tbl_name}'"
            ))
            db_cols = {row[0] for row in r.fetchall()}

            if not db_cols:
                print(f"  [TABLE MISSING] {tbl_name}")
                missing_total += 1
                continue

            missing = model_cols - db_cols
            extra   = db_cols - model_cols
            if missing:
                print(f"\n  [MISSING COLS] {tbl_name}: {sorted(missing)}")
                missing_total += len(missing)

    if missing_total == 0:
        print("All model columns exist in DB.")
    else:
        print(f"\nTotal issues: {missing_total}")
    await engine.dispose()

asyncio.run(main())
