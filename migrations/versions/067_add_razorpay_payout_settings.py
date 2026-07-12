"""067_add_razorpay_payout_settings

Revision ID: 067
Revises: 066
Create Date: 2026-07-12

Seeds the system_settings table with Razorpay Payout (X) API config keys
so admins can configure auto-payout via the Settings -> Payment tab.

Keys added (value starts empty / false):
  razorpay_payout_enabled     - toggle: use Razorpay X for auto-payout
  razorpay_x_key_id           - Razorpay X API Key ID (rzp_live_...)
  razorpay_x_key_secret       - Razorpay X API Key Secret (stored encrypted)
  razorpay_x_account_number   - Fund account number linked to your Razorpay X account
  withdrawal_payout_mode      - manual or razorpay (default: manual)
"""
from alembic import op
import sqlalchemy as sa

revision = '067'
down_revision = '066'
branch_labels = None
depends_on = None

# (group_val, key, value, is_secret, label)
_KEYS = [
    ('payment', 'razorpay_payout_enabled',   'false',  False, 'Enable automatic payouts via Razorpay X'),
    ('payment', 'razorpay_x_key_id',         '',       False, 'Razorpay X API Key ID (rzp_live_...)'),
    ('payment', 'razorpay_x_key_secret',     '',       True,  'Razorpay X API Key Secret - stored encrypted'),
    ('payment', 'razorpay_x_account_number', '',       False, 'Razorpay X fund account number'),
    ('payment', 'withdrawal_payout_mode',    'manual', False, 'Payout mode: manual or razorpay'),
]


def upgrade():
    conn = op.get_bind()
    for group_val, key, value, is_secret, label in _KEYS:
        conn.execute(sa.text(
            'INSERT INTO system_settings ("group", key, value, is_secret, label) '
            'VALUES (:group_val, :key, :value, :is_secret, :label) '
            'ON CONFLICT ("group", key) DO NOTHING'
        ), {
            'group_val': group_val,
            'key': key,
            'value': value,
            'is_secret': is_secret,
            'label': label,
        })


def downgrade():
    conn = op.get_bind()
    for group_val, key, *_ in _KEYS:
        conn.execute(
            sa.text('DELETE FROM system_settings WHERE "group" = :group_val AND key = :key'),
            {'group_val': group_val, 'key': key}
        )
