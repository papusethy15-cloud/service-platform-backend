"""
City/Area schema migration — run once:  python migrate_city.py
Adds: zones, city_settings tables + new columns on cities & areas.
"""
import asyncio
from sqlalchemy import text
from app.core.database import engine

MIGRATIONS = [
    ("cities.latitude",       "ALTER TABLE cities ADD COLUMN IF NOT EXISTS latitude FLOAT"),
    ("cities.longitude",      "ALTER TABLE cities ADD COLUMN IF NOT EXISTS longitude FLOAT"),
    ("cities.is_serviceable", "ALTER TABLE cities ADD COLUMN IF NOT EXISTS is_serviceable BOOLEAN DEFAULT TRUE"),
    ("zones table", """
        CREATE TABLE IF NOT EXISTS zones (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            city_id     UUID NOT NULL REFERENCES cities(id),
            name        VARCHAR(150) NOT NULL,
            description TEXT,
            created_at  TIMESTAMP DEFAULT NOW(),
            updated_at  TIMESTAMP DEFAULT NOW(),
            is_active   BOOLEAN DEFAULT TRUE
        )
    """),
    ("areas.zone_id",          "ALTER TABLE areas ADD COLUMN IF NOT EXISTS zone_id UUID REFERENCES zones(id)"),
    ("areas.latitude",         "ALTER TABLE areas ADD COLUMN IF NOT EXISTS latitude FLOAT"),
    ("areas.longitude",        "ALTER TABLE areas ADD COLUMN IF NOT EXISTS longitude FLOAT"),
    ("areas.surge_multiplier", "ALTER TABLE areas ADD COLUMN IF NOT EXISTS surge_multiplier FLOAT DEFAULT 1.0"),
    ("city_settings table", """
        CREATE TABLE IF NOT EXISTS city_settings (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            city_id                 UUID NOT NULL UNIQUE REFERENCES cities(id),
            min_booking_amount      FLOAT DEFAULT 0.0,
            max_booking_amount      FLOAT,
            booking_advance_days    INTEGER DEFAULT 7,
            cancellation_window_hrs INTEGER DEFAULT 2,
            auto_assign_enabled     BOOLEAN DEFAULT TRUE,
            notes                   TEXT,
            created_at              TIMESTAMP DEFAULT NOW(),
            updated_at              TIMESTAMP DEFAULT NOW(),
            is_active               BOOLEAN DEFAULT TRUE
        )
    """),
]

async def main():
    async with engine.begin() as conn:
        for name, sql in MIGRATIONS:
            print(f"  {name} ...", end=" ")
            try:
                await conn.execute(text(sql))
                print("OK")
            except Exception as e:
                print(f"skipped ({str(e)[:60]})")
    print("\nCity migration complete.")

asyncio.run(main())
