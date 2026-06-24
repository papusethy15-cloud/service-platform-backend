"""create domain_cities table — links cities to a domain (website city scoping)

Revision ID: 024_domain_cities
Revises: 023_wallet_balance_before
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '024_domain_cities'
down_revision = '023_wallet_balance_before'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    def table_exists(name):
        return bind.execute(sa.text(
            f"SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema='public' AND table_name='{name}')"
        )).scalar()

    if not table_exists('domain_cities'):
        op.create_table('domain_cities',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('domain_id',  postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('domains.id'), nullable=False),
            sa.Column('city_id',    postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('cities.id'), nullable=False),
            sa.Column('sort_order', sa.Integer, server_default='0'),
            sa.Column('is_active',  sa.Boolean, server_default='true'),
            sa.Column('created_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at', sa.DateTime, server_default=sa.text('NOW()')),
            sa.UniqueConstraint('domain_id', 'city_id', name='uq_domain_city'),
        )


def downgrade():
    op.drop_table('domain_cities')
