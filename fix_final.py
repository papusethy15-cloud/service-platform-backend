import asyncio, sys
sys.path.insert(0, r'C:\MyWorkspace\Palei Solutions\backend')
from sqlalchemy import text
from app.core.database import engine

FIXES = [
    ("technician_availability.is_active",
        "ALTER TABLE technician_availability ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"),
    ("technician_availability.updated_at",
        "ALTER TABLE technician_availability ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE"),
]

async def main():
    async with engine.begin() as conn:
        for name, sql in FIXES:
            print(f"  {name} ...", end=" ")
            try:
                await conn.execute(text(sql)); print("OK")
            except Exception as e:
                print(f"skipped ({str(e)[:60]})")
    await engine.dispose()
    print("Done.")

asyncio.run(main())
