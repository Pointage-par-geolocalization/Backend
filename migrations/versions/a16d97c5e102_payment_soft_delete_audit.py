"""Add audited soft delete fields to payments."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a16d97c5e102"
down_revision: Union[str, Sequence[str], None] = "0be719ba5a41"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("payments", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("deleted_by", sa.UUID(), nullable=True))
    op.add_column("payments", sa.Column("deletion_reason", sa.Text(), nullable=True))
    op.create_index("ix_payments_deleted_at", "payments", ["deleted_at"], unique=False)

def downgrade() -> None:
    op.drop_index("ix_payments_deleted_at", table_name="payments")
    op.drop_column("payments", "deletion_reason")
    op.drop_column("payments", "deleted_by")
    op.drop_column("payments", "deleted_at")
