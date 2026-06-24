-- Run this in pgAdmin or any PostgreSQL client connected to palei_solutions
-- Upserts the test admin user (email+password login)

INSERT INTO users (id, name, mobile, email, password_hash, role, is_verified, is_active, created_at, updated_at)
VALUES (
    gen_random_uuid(),
    'Super Admin',
    '9999999999',
    'admin@paleisolutions.com',
    '$2b$12$SDijOEnGzmRqylue4oaEn.aC2TGbKzWFV2h5ocbDR1g0FSemfVoDO',
    'SUPER_ADMIN',
    true,
    true,
    now(),
    now()
)
ON CONFLICT (email) DO UPDATE SET
    password_hash = EXCLUDED.password_hash,
    role          = 'SUPER_ADMIN',
    name          = 'Super Admin',
    is_verified   = true,
    is_active     = true,
    updated_at    = now();

-- Verify
SELECT id, name, email, role, is_verified, is_active FROM users WHERE email = 'admin@paleisolutions.com';
