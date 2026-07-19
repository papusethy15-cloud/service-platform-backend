"""Add includes_json, excludes_json, faqs_json to domain_service_overrides

Revision ID: 075
Revises: 074_add_customer_review_fields
Create Date: 2026-07-19
"""
from alembic import op
import sqlalchemy as sa

revision = '075'
down_revision = '074_add_customer_review_fields'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    for col in ('includes_json', 'excludes_json', 'faqs_json'):
        exists = conn.execute(sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='domain_service_overrides' AND column_name=:col"
        ), {"col": col}).fetchone()
        if not exists:
            op.add_column(
                'domain_service_overrides',
                sa.Column(col, sa.Text, nullable=True)
            )


def downgrade():
    for col in ('includes_json', 'excludes_json', 'faqs_json'):
        op.drop_column('domain_service_overrides', col)
