"""026 — platform branding settings keys (no schema change needed, system_settings table exists)

Revision ID: 026_platform_settings
Revises: 025_coupon_domain_id
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa

revision = '026_platform_settings'
down_revision = '025_coupon_domain_id'
branch_labels = None
depends_on = None

def upgrade():
    # Insert default platform setting rows if they don't exist
    op.execute("""
        INSERT INTO system_settings (id, "group", key, value, is_secret, label, created_at, updated_at, is_active)
        VALUES
          (gen_random_uuid(), 'platform', 'app_name',      'Palei Solutions',  false, 'Platform Name',     now(), now(), true),
          (gen_random_uuid(), 'platform', 'tagline',       'Home Services Platform', false, 'Tagline', now(), now(), true),
          (gen_random_uuid(), 'platform', 'logo_url',      '',  false, 'Logo URL',          now(), now(), true),
          (gen_random_uuid(), 'platform', 'favicon_url',   '',  false, 'Favicon URL',       now(), now(), true),
          (gen_random_uuid(), 'platform', 'primary_color', '#1B4FD8', false, 'Primary Color', now(), now(), true),
          (gen_random_uuid(), 'platform', 'support_email', 'support@palei.in', false, 'Support Email', now(), now(), true),
          (gen_random_uuid(), 'platform', 'support_phone', '',  false, 'Support Phone',     now(), now(), true),
          (gen_random_uuid(), 'platform', 'address',       '',  false, 'Business Address',  now(), now(), true),
          (gen_random_uuid(), 'platform', 'website_url',   '',  false, 'Website URL',       now(), now(), true),
          (gen_random_uuid(), 'platform', 'gst_number',    '',  false, 'GST Number',        now(), now(), true),
          (gen_random_uuid(), 'platform', 'currency',      'INR', false, 'Currency',        now(), now(), true),
          (gen_random_uuid(), 'platform', 'timezone',      'Asia/Kolkata', false, 'Timezone', now(), now(), true)
        ON CONFLICT ("group", key) DO NOTHING;
    """)

def downgrade():
    op.execute("DELETE FROM system_settings WHERE \"group\" = 'platform';")
