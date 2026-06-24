"""Run once: python migrate_callback_tracking.py

Adds visitor-context columns to callback_requests so that when a callback
is requested by a mobile number with no matching customer record, the
admin still has IP / location / page / user-agent context before calling.
"""
import asyncio
from sqlalchemy import text
from app.core.database import engine

MIGRATIONS = [
    ("callback_requests.domain_id", """
        ALTER TABLE callback_requests ADD COLUMN IF NOT EXISTS domain_id UUID
    """),
    ("callback_requests.page_url", """
        ALTER TABLE callback_requests ADD COLUMN IF NOT EXISTS page_url VARCHAR(500)
    """),
    ("callback_requests.ip_address", """
        ALTER TABLE callback_requests ADD COLUMN IF NOT EXISTS ip_address VARCHAR(64)
    """),
    ("callback_requests.user_agent", """
        ALTER TABLE callback_requests ADD COLUMN IF NOT EXISTS user_agent VARCHAR(500)
    """),
    ("callback_requests.location", """
        ALTER TABLE callback_requests ADD COLUMN IF NOT EXISTS location VARCHAR(255)
    """),
    ("callback_requests.domain_id index", """
        CREATE INDEX IF NOT EXISTS ix_callback_requests_domain_id ON callback_requests (domain_id)
    """),
]

async def main():
    async with engine.begin() as conn:
        for name, sql in MIGRATIONS:
            print(f"  {name}...", end=" ")
            try:
                await conn.execute(text(sql)); print("OK")
            except Exception as e:
                print(f"skipped ({str(e)[:60]})")
    print("Callback tracking migration done.")

asyncio.run(main())
