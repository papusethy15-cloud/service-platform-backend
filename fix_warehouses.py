import asyncio, sys
sys.path.insert(0, r'C:\MyWorkspace\Palei Solutions\backend')
from sqlalchemy import text
from app.core.database import engine

FIXES = [
    ("warehouses.city",     "ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS city VARCHAR(100)"),
    ("warehouses.address",  "ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS address TEXT"),
]

async def main():
    async with engine.begin() as conn:
        # First dump current columns
        r = await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='warehouses' ORDER BY ordinal_position"
        ))
        cols = [row[0] for row in r.fetchall()]
        print("Current warehouses columns:", cols)

        for name, sql in FIXES:
            print(f"  {name} ...", end=" ", flush=True)
            try:
                await conn.execute(text(sql))
                print("OK")
            except Exception as e:
                print(f"skipped: {str(e)[:80]}")
    await engine.dispose()
    print("Done.")

asyncio.run(main())
