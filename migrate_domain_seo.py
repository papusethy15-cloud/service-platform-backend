"""Run once: python migrate_domain_seo.py"""
import asyncio
from sqlalchemy import text
from app.core.database import engine

MIGRATIONS = [
    ("domain_seo table", """
        CREATE TABLE IF NOT EXISTS domain_seo (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id        UUID NOT NULL UNIQUE REFERENCES domains(id),
            meta_title       VARCHAR(200),
            meta_description TEXT,
            meta_keywords    TEXT,
            og_title         VARCHAR(200),
            og_description   TEXT,
            og_image_url     VARCHAR(500),
            canonical_url    VARCHAR(500),
            robots           VARCHAR(100) DEFAULT 'index,follow',
            schema_json      TEXT,
            created_at       TIMESTAMP DEFAULT NOW(),
            updated_at       TIMESTAMP DEFAULT NOW(),
            is_active        BOOLEAN DEFAULT TRUE
        )
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
    print("Domain SEO migration done.")

asyncio.run(main())
