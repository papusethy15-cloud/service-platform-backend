"""Add domain_profiles table and system_settings table

Revision ID: 013_domain_profile_system_settings
Revises: 012_public_booking_fields
Create Date: 2026-06-15

domain_profiles  — rich per-domain branding, social media, office address, invoice/bank details
system_settings  — key-value store for platform credentials (Cloudinary, Razorpay, SMS, etc.)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
import uuid

revision = '013_domain_profile_system_settings'
down_revision = '012_public_booking_fields'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── system_settings ──────────────────────────────────────────
    op.create_table(
        'system_settings',
        sa.Column('id',         UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('group',      sa.String(50),  nullable=False),
        sa.Column('key',        sa.String(100), nullable=False),
        sa.Column('value',      sa.Text(),      nullable=True),
        sa.Column('is_secret',  sa.Boolean(),   default=False),
        sa.Column('label',      sa.String(200), nullable=True),
        sa.Column('is_active',  sa.Boolean(),   default=True),
        sa.Column('created_at', sa.DateTime(),  nullable=True),
        sa.Column('updated_at', sa.DateTime(),  nullable=True),
        sa.UniqueConstraint('group', 'key', name='uq_setting_group_key'),
    )

    # ── domain_profiles ──────────────────────────────────────────
    op.create_table(
        'domain_profiles',
        sa.Column('id',         UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('domain_id',  UUID(as_uuid=True), sa.ForeignKey('domains.id'), nullable=False),
        # media
        sa.Column('logo_url',       sa.String(500), nullable=True),
        sa.Column('logo_dark_url',  sa.String(500), nullable=True),
        sa.Column('favicon_url',    sa.String(500), nullable=True),
        sa.Column('og_image_url',   sa.String(500), nullable=True),
        sa.Column('banner_url',     sa.String(500), nullable=True),
        # social
        sa.Column('facebook_url',   sa.String(500), nullable=True),
        sa.Column('instagram_url',  sa.String(500), nullable=True),
        sa.Column('twitter_url',    sa.String(500), nullable=True),
        sa.Column('youtube_url',    sa.String(500), nullable=True),
        sa.Column('linkedin_url',   sa.String(500), nullable=True),
        sa.Column('whatsapp_number',sa.String(20),  nullable=True),
        # contact
        sa.Column('support_phone',  sa.String(30),  nullable=True),
        sa.Column('support_email',  sa.String(200), nullable=True),
        sa.Column('office_address', sa.Text(),      nullable=True),
        sa.Column('office_city',    sa.String(100), nullable=True),
        sa.Column('office_state',   sa.String(100), nullable=True),
        sa.Column('office_pincode', sa.String(10),  nullable=True),
        sa.Column('office_country', sa.String(100), default='India'),
        sa.Column('google_maps_url',sa.String(500), nullable=True),
        # invoice / business
        sa.Column('business_legal_name',  sa.String(200), nullable=True),
        sa.Column('gstin',                sa.String(20),  nullable=True),
        sa.Column('pan_number',           sa.String(20),  nullable=True),
        sa.Column('invoice_prefix',       sa.String(20),  nullable=True),
        sa.Column('bank_account_name',    sa.String(200), nullable=True),
        sa.Column('bank_account_number',  sa.String(50),  nullable=True),
        sa.Column('bank_ifsc',            sa.String(20),  nullable=True),
        sa.Column('bank_name',            sa.String(100), nullable=True),
        sa.Column('bank_branch',          sa.String(200), nullable=True),
        sa.Column('upi_id',               sa.String(100), nullable=True),
        # about
        sa.Column('tagline',         sa.String(300), nullable=True),
        sa.Column('about_short',     sa.Text(),      nullable=True),
        sa.Column('copyright_text',  sa.String(300), nullable=True),
        # base fields
        sa.Column('is_active',  sa.Boolean(), default=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('domain_id', name='uq_domain_profile'),
    )


def downgrade() -> None:
    op.drop_table('domain_profiles')
    op.drop_table('system_settings')
