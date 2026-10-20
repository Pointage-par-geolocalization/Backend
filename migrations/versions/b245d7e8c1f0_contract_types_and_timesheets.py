"""Add employee contract categories and persisted weekly timesheets."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b245d7e8c1f0"
down_revision: Union[str, Sequence[str], None] = "a16d97c5e102"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("employees", sa.Column("contract_type", sa.String(length=16), nullable=False, server_default="JOURNALIER"))
    op.add_column("employees", sa.Column("monthly_salary", sa.Integer(), nullable=True))
    op.alter_column("employees", "contract_type", server_default=None)
    op.create_check_constraint("ck_employees_contract_type", "employees", "contract_type IN ('JOURNALIER','CDI')")
    op.create_check_constraint("ck_employees_monthly_salary", "employees", "contract_type != 'CDI' OR monthly_salary > 0")
    op.create_table(
        "timesheets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_code", sa.String(length=32), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("entries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manager_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('DRAFT','SUBMITTED','RETURNED','APPROVED')", name="ck_timesheet_status"),
        sa.ForeignKeyConstraint(["employee_code"], ["employees.employee_code"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_code", "week_start", name="uq_timesheet_employee_week"),
    )
    op.create_index("ix_timesheets_employee_code", "timesheets", ["employee_code"], unique=False)
    op.create_index("ix_timesheets_week_status", "timesheets", ["week_start", "status"], unique=False)

def downgrade() -> None:
    op.drop_index("ix_timesheets_week_status", table_name="timesheets")
    op.drop_index("ix_timesheets_employee_code", table_name="timesheets")
    op.drop_table("timesheets")
    op.drop_constraint("ck_employees_monthly_salary", "employees", type_="check")
    op.drop_constraint("ck_employees_contract_type", "employees", type_="check")
    op.drop_column("employees", "monthly_salary")
    op.drop_column("employees", "contract_type")
