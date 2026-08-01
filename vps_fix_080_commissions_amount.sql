-- ============================================================
-- VPS FIX: Drop stale `amount NOT NULL` column from commissions
-- Root cause of: NotNullViolationError on /api/v1/bookings/.../settle
--
-- Run on VPS:
--   psql postgresql://palei_user:SrikantaDB1994@localhost:5432/palei_solutions -f vps_fix_080_commissions_amount.sql
-- ============================================================

\echo '>>> Checking commissions table columns before fix...'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'commissions'
ORDER BY ordinal_position;

\echo ''
\echo '>>> Dropping stale columns (IF EXISTS = safe to re-run)...'

-- THE bug column — NOT NULL, never written by current code
ALTER TABLE commissions DROP COLUMN IF EXISTS amount;

-- Orphan columns from migration 001 that model no longer has
ALTER TABLE commissions DROP COLUMN IF EXISTS commission_type;
ALTER TABLE commissions DROP COLUMN IF EXISTS is_active;
ALTER TABLE commissions DROP COLUMN IF EXISTS updated_at;

\echo ''
\echo '>>> commissions table after fix:'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'commissions'
ORDER BY ordinal_position;

-- Also stamp the alembic version so migration chain stays consistent
UPDATE alembic_version SET version_num = '080'
WHERE version_num = '079';

\echo ''
\echo '>>> DONE. Restart palei-backend with: pm2 restart palei-backend'
