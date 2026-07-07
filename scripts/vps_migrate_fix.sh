#!/bin/bash
# VPS Migration Fix Script
# Run this ONCE on the VPS after git pull to fix the 048/049 migration crash.
# Usage: cd /opt/backend/bibekenterprises-backend && bash scripts/vps_migrate_fix.sh

set -e

echo "=== VPS Migration Fix ==="
echo "Step 1: Activating venv..."
source venv/bin/activate

echo ""
echo "Step 2: Current alembic_version on VPS DB:"
python3 -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.core.config import settings

DB_URL = settings.DATABASE_URL.replace('postgresql://', 'postgresql+asyncpg://')

async def check():
    engine = create_async_engine(DB_URL)
    async with engine.connect() as conn:
        result = await conn.execute(text('SELECT version_num FROM alembic_version'))
        rows = result.fetchall()
        print('  alembic_version rows:', [r[0] for r in rows])
    await engine.dispose()

asyncio.run(check())
"

echo ""
echo "Step 3: Running alembic upgrade head..."
echo "  (env.py will auto-reset alembic_version to 046 if needed)"
alembic upgrade head

echo ""
echo "Step 4: Final alembic_version after upgrade:"
python3 -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.core.config import settings

DB_URL = settings.DATABASE_URL.replace('postgresql://', 'postgresql+asyncpg://')

async def check():
    engine = create_async_engine(DB_URL)
    async with engine.connect() as conn:
        result = await conn.execute(text('SELECT version_num FROM alembic_version'))
        rows = result.fetchall()
        print('  alembic_version rows:', [r[0] for r in rows])
    await engine.dispose()

asyncio.run(check())
"

echo ""
echo "=== Done! Now restart the backend service ==="
echo "  sudo systemctl restart bibekenterprises-backend"
