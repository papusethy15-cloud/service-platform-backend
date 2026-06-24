-- ============================================================
-- FIX: Alembic multiple-heads / overlaps error
-- Run this in pgAdmin Query Tool or psql
-- ============================================================

-- Step 1: See what is currently tracked
SELECT version_num FROM alembic_version ORDER BY version_num;

-- Step 2: Clear stale rows and stamp correct single head
-- (This does NOT touch any real tables — only Alembic bookkeeping)
DELETE FROM alembic_version;
INSERT INTO alembic_version (version_num) VALUES ('011_purchase_orders');

-- Step 3: Verify
SELECT version_num FROM alembic_version;

-- After running this SQL, go back to PowerShell and run:
--   alembic upgrade heads
