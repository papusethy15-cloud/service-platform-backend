-- Migration: Add commission_group_part_rules table + booking settlement commission records
-- Run once against the database.

CREATE TABLE IF NOT EXISTS commission_group_part_rules (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id            UUID NOT NULL REFERENCES commission_groups(id) ON DELETE CASCADE,
    part_name_match     VARCHAR(200),          -- NULL = all parts; keyword substring match
    part_source_filter  VARCHAR(30),           -- NULL | OFFICE_STOCK | MARKET_PURCHASE
    commission_type     VARCHAR(20) NOT NULL DEFAULT 'PERCENTAGE',
    rate                FLOAT       NOT NULL DEFAULT 0.0,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Add booking-level settlement columns to commissions table (if not already present)
ALTER TABLE commissions
    ADD COLUMN IF NOT EXISTS item_type     VARCHAR(20),   -- SERVICE | PART
    ADD COLUMN IF NOT EXISTS item_name     VARCHAR(300),
    ADD COLUMN IF NOT EXISTS item_quantity INTEGER DEFAULT 1,
    ADD COLUMN IF NOT EXISTS part_source   VARCHAR(30);   -- OFFICE_STOCK | MARKET_PURCHASE
