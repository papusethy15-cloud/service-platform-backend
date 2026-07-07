"""039_add_document_type_fields

Add id_proof_url, id_proof_type, address_proof_url, and address_proof_type
to the users table.

- id_proof_url / address_proof_url were added ad-hoc before Alembic
  tracking was in place and were therefore missing from the live DB.
  They are added here with IF NOT EXISTS so this is a safe no-op if
  they already exist (e.g. on a DB that had the ad-hoc script applied).

- id_proof_type / address_proof_type are brand-new columns that store
  the human-readable document label (e.g. "Aadhaar Card", "PAN Card").

Revision ID: 039
Revises: 038
"""
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade():
    # URL columns — may already exist on older dev DBs, hence IF NOT EXISTS
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_url VARCHAR(500)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_url VARCHAR(500)")
    # Type label columns — brand new
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_type VARCHAR(50)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_type VARCHAR(50)")


def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS id_proof_url")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS address_proof_url")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS id_proof_type")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS address_proof_type")
