"""Add payment type, status and optional site dimensions.

Existing payment rows remain settled (PAYE) so historical salary balances are unchanged.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f2a75c19d1e4"
down_revision = "e7f10c3a9b42"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payments", sa.Column("payment_type", sa.String(length=50), nullable=False, server_default="SALAIRE"))
    op.add_column("payments", sa.Column("status", sa.String(length=16), nullable=False, server_default="PAYE"))
    op.add_column("payments", sa.Column("site_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_payments_site_id_sites", "payments", "sites", ["site_id"], ["id"], ondelete="SET NULL")
    op.create_check_constraint("ck_payments_status", "payments", "status IN ('PAYE','EN_ATTENTE','ANNULE')")
    op.create_index("ix_payments_status_created_at", "payments", ["status", "created_at"])
    op.create_index("ix_payments_site_id", "payments", ["site_id"])


def downgrade():
    op.drop_index("ix_payments_site_id", table_name="payments")
    op.drop_index("ix_payments_status_created_at", table_name="payments")
    op.drop_constraint("ck_payments_status", "payments", type_="check")
    op.drop_constraint("fk_payments_site_id_sites", "payments", type_="foreignkey")
    op.drop_column("payments", "site_id")
    op.drop_column("payments", "status")
    op.drop_column("payments", "payment_type")
