"""Add immutable public employee numbers; preserve legacy employee_code."""
from alembic import op
import sqlalchemy as sa
revision = "d4e9a2f1c8b7"
down_revision = "b245d7e8c1f0"
branch_labels = None
depends_on = None
def upgrade():
    op.execute("CREATE SEQUENCE employee_number_seq START 1")
    op.add_column("employees", sa.Column("employee_number", sa.String(16), nullable=True))
    op.execute("WITH numbered AS (SELECT employee_code, row_number() OVER (ORDER BY created_at, employee_code) n FROM employees) UPDATE employees e SET employee_number='ID-' || lpad(numbered.n::text,3,'0') FROM numbered WHERE e.employee_code=numbered.employee_code")
    op.execute("SELECT setval('employee_number_seq', GREATEST(COALESCE((SELECT count(*) FROM employees),0),1), COALESCE((SELECT count(*) FROM employees),0)>0)")
    op.alter_column("employees", "employee_number", nullable=False)
    op.create_unique_constraint("uq_employees_employee_number", "employees", ["employee_number"])
def downgrade():
    op.drop_constraint("uq_employees_employee_number", "employees", type_="unique")
    op.drop_column("employees", "employee_number")
    op.execute("DROP SEQUENCE employee_number_seq")
