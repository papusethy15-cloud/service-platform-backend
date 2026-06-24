"""create missing domain tables: domain_categories, domain_seo, service_city_prices

Revision ID: 004_domain_tables
Revises: 003_fix_cities
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '004_domain_tables'
down_revision = '003_fix_cities'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    def table_exists(name):
        return bind.execute(sa.text(
            f"SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema='public' AND table_name='{name}')"
        )).scalar()

    if not table_exists('domain_categories'):
        op.create_table('domain_categories',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('domain_id',   postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('domains.id'), nullable=False),
            sa.Column('category_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('service_categories.id'), nullable=False),
            sa.Column('sort_order',  sa.Integer, server_default='0'),
            sa.Column('is_active',   sa.Boolean, server_default='true'),
            sa.Column('created_at',  sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at',  sa.DateTime, server_default=sa.text('NOW()')),
            sa.UniqueConstraint('domain_id', 'category_id', name='uq_domain_category'),
        )

    if not table_exists('domain_seo'):
        op.create_table('domain_seo',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('domain_id',        postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('domains.id'), nullable=False),
            sa.Column('meta_title',       sa.String(200), nullable=True),
            sa.Column('meta_description', sa.Text,        nullable=True),
            sa.Column('meta_keywords',    sa.Text,        nullable=True),
            sa.Column('og_title',         sa.String(200), nullable=True),
            sa.Column('og_description',   sa.Text,        nullable=True),
            sa.Column('og_image_url',     sa.String(500), nullable=True),
            sa.Column('canonical_url',    sa.String(500), nullable=True),
            sa.Column('robots',           sa.String(100), server_default='index,follow'),
            sa.Column('schema_json',      sa.Text,        nullable=True),
            sa.Column('is_active',        sa.Boolean,     server_default='true'),
            sa.Column('created_at',       sa.DateTime,    server_default=sa.text('NOW()')),
            sa.Column('updated_at',       sa.DateTime,    server_default=sa.text('NOW()')),
            sa.UniqueConstraint('domain_id', name='uq_domain_seo'),
        )

    if not table_exists('service_city_prices'):
        op.create_table('service_city_prices',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text('gen_random_uuid()')),
            sa.Column('service_id',   postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('services.id'), nullable=False),
            sa.Column('city_id',      postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('cities.id'), nullable=False),
            sa.Column('price',        sa.Float,   nullable=False),
            sa.Column('is_available', sa.Boolean, server_default='true'),
            sa.Column('is_active',    sa.Boolean, server_default='true'),
            sa.Column('created_at',   sa.DateTime, server_default=sa.text('NOW()')),
            sa.Column('updated_at',   sa.DateTime, server_default=sa.text('NOW()')),
            sa.UniqueConstraint('service_id', 'city_id', name='uq_service_city_price'),
        )


def downgrade():
    op.drop_table('service_city_prices')
    op.drop_table('domain_seo')
    op.drop_table('domain_categories')
