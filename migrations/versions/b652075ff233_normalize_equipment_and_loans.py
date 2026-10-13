"""normalize equipment and loans

Revision ID: b652075ff233
Revises: c12c6765d713
Create Date: 2026-10-09 18:02:45.469078
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Sequence, Union
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision: str = "b652075ff233"
down_revision: Union[str, Sequence[str], None] = "c12c6765d713"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid(value):
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _dt(value, default=None):
    if not value:
        return default
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return default


def _date(value):
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _iso(value):
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def upgrade() -> None:
    op.create_table(
        "equipment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("serial_number", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("DISPONIBLE", "EMPRUNTE", "HORS_SERVICE", name="equipment_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "equipment_loans",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("equipment_id", sa.UUID(), nullable=True),
        sa.Column("equipment_name", sa.Text(), nullable=False),
        sa.Column("employee_code", sa.String(length=32), nullable=True),
        sa.Column("employee_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Enum("DEMANDE", "REFUSEE", "ANNULEE", "EN_COURS", "RETOUR_DEMANDE", "RETOURNE", name="loan_status"), nullable=False),
        sa.Column("request_note", sa.Text(), nullable=True),
        sa.Column("expected_return_date", sa.Date(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.Column("loaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("return_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("return_note", sa.Text(), nullable=True),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["employee_code"], ["employees.employee_code"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["equipment_id"], ["equipment.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_equipment_loans_employee_requested", "equipment_loans", ["employee_code", "requested_at"], unique=False)
    op.create_index("ix_equipment_loans_status", "equipment_loans", ["status"], unique=False)
    op.create_index("uq_equipment_loan_one_active", "equipment_loans", ["equipment_id"], unique=True, postgresql_where=sa.text("equipment_id IS NOT NULL AND status IN ('EN_COURS','RETOUR_DEMANDE')"))
    op.create_index("uq_equipment_loan_one_pending_per_employee", "equipment_loans", ["equipment_id", "employee_code"], unique=True, postgresql_where=sa.text("status = 'DEMANDE' AND equipment_id IS NOT NULL AND employee_code IS NOT NULL"))

    conn = op.get_bind()
    old_equipment = [dict(row) for row in conn.execute(sa.text("SELECT payload FROM domain_records WHERE domain='equipment'")).mappings()]
    old_loans = [dict(row) for row in conn.execute(sa.text("SELECT payload FROM domain_records WHERE domain='loans'")).mappings()]
    employee_codes = set(conn.execute(sa.text("SELECT employee_code FROM employees")).scalars())
    user_ids = set(conn.execute(sa.text("SELECT id FROM users")).scalars())
    equipment_ids: set[UUID] = set()

    for row in old_equipment:
        item = row.get("payload") or {}
        equipment_id = _uuid(item.get("id"))
        if equipment_id is None:
            raise RuntimeError(f"ID matériel JSONB non UUID: {item.get('id')!r}")
        status = item.get("status") or "DISPONIBLE"
        if status not in {"DISPONIBLE", "EMPRUNTE", "HORS_SERVICE"}:
            raise RuntimeError(f"Statut matériel non reconnu pour {equipment_id}: {status!r}")
        conn.execute(sa.text("""
            INSERT INTO equipment (id,name,category,serial_number,description,status,created_at)
            VALUES (:id,:name,:category,:serial_number,:description,:status,:created_at)
        """), {
            "id": equipment_id,
            "name": str(item.get("name") or "Matériel sans nom"),
            "category": item.get("category"),
            "serial_number": item.get("serialNumber"),
            "description": item.get("description"),
            "status": status,
            "created_at": _dt(item.get("createdAt"), datetime.now(timezone.utc)),
        })
        equipment_ids.add(equipment_id)

    valid_statuses = {"DEMANDE", "REFUSEE", "ANNULEE", "EN_COURS", "RETOUR_DEMANDE", "RETOURNE"}
    for row in old_loans:
        item = row.get("payload") or {}
        loan_id = _uuid(item.get("id"))
        if loan_id is None:
            raise RuntimeError(f"ID de prêt JSONB non UUID: {item.get('id')!r}")
        status = item.get("status") or "DEMANDE"
        if status not in valid_statuses:
            raise RuntimeError(f"Statut de prêt non reconnu pour {loan_id}: {status!r}")
        equipment_id = _uuid(item.get("equipmentId"))
        if equipment_id not in equipment_ids:
            equipment_id = None
        employee_code = item.get("employeeCode")
        if employee_code not in employee_codes:
            employee_code = None
        decided_by = _uuid(item.get("decidedBy"))
        if decided_by not in user_ids:
            decided_by = None
        confirmed_by = _uuid(item.get("confirmedBy"))
        if confirmed_by not in user_ids:
            confirmed_by = None
        conn.execute(sa.text("""
            INSERT INTO equipment_loans (
                id,equipment_id,equipment_name,employee_code,employee_name,status,request_note,
                expected_return_date,requested_at,decided_at,decided_by,refusal_reason,loaned_at,
                return_requested_at,return_note,returned_at,confirmed_by
            ) VALUES (
                :id,:equipment_id,:equipment_name,:employee_code,:employee_name,:status,:request_note,
                :expected_return_date,:requested_at,:decided_at,:decided_by,:refusal_reason,:loaned_at,
                :return_requested_at,:return_note,:returned_at,:confirmed_by
            )
        """), {
            "id": loan_id,
            "equipment_id": equipment_id,
            "equipment_name": str(item.get("equipmentName") or "Matériel supprimé"),
            "employee_code": employee_code,
            "employee_name": str(item.get("employeeName") or employee_code or "Salarié supprimé"),
            "status": status,
            "request_note": item.get("requestNote"),
            "expected_return_date": _date(item.get("expectedReturnDate")),
            "requested_at": _dt(item.get("requestedAt"), datetime.now(timezone.utc)),
            "decided_at": _dt(item.get("decidedAt")),
            "decided_by": decided_by,
            "refusal_reason": item.get("refusalReason"),
            "loaned_at": _dt(item.get("loanedAt")),
            "return_requested_at": _dt(item.get("returnRequestedAt")),
            "return_note": item.get("returnNote"),
            "returned_at": _dt(item.get("returnedAt")),
            "confirmed_by": confirmed_by,
        })
    conn.execute(sa.text("DELETE FROM domain_records WHERE domain IN ('equipment','loans')"))


def downgrade() -> None:
    conn = op.get_bind()
    for row in conn.execute(sa.text("SELECT * FROM equipment")).mappings():
        item = {
            "id": str(row["id"]), "name": row["name"], "category": row["category"],
            "serialNumber": row["serial_number"], "description": row["description"],
            "status": row["status"], "createdAt": _iso(row["created_at"]),
        }
        conn.execute(sa.text("""
            INSERT INTO domain_records (id,domain,payload,updated_at)
            VALUES (:id,'equipment',CAST(:payload AS jsonb),now())
            ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=now()
        """), {"id": f"equipment:{item['id']}", "payload": json.dumps(item)})
    for row in conn.execute(sa.text("SELECT * FROM equipment_loans")).mappings():
        item = {
            "id": str(row["id"]), "equipmentId": str(row["equipment_id"]) if row["equipment_id"] else None,
            "equipmentName": row["equipment_name"], "employeeCode": row["employee_code"],
            "employeeName": row["employee_name"], "status": row["status"],
            "requestNote": row["request_note"], "expectedReturnDate": _iso(row["expected_return_date"]),
            "requestedAt": _iso(row["requested_at"]), "decidedAt": _iso(row["decided_at"]),
            "refusalReason": row["refusal_reason"], "loanedAt": _iso(row["loaned_at"]),
            "returnRequestedAt": _iso(row["return_requested_at"]), "returnNote": row["return_note"],
            "returnedAt": _iso(row["returned_at"]),
        }
        conn.execute(sa.text("""
            INSERT INTO domain_records (id,domain,payload,updated_at)
            VALUES (:id,'loans',CAST(:payload AS jsonb),now())
            ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=now()
        """), {"id": f"loans:{item['id']}", "payload": json.dumps(item)})
    op.drop_index("uq_equipment_loan_one_pending_per_employee", table_name="equipment_loans", postgresql_where=sa.text("status = 'DEMANDE' AND equipment_id IS NOT NULL AND employee_code IS NOT NULL"))
    op.drop_index("uq_equipment_loan_one_active", table_name="equipment_loans", postgresql_where=sa.text("equipment_id IS NOT NULL AND status IN ('EN_COURS','RETOUR_DEMANDE')"))
    op.drop_index("ix_equipment_loans_status", table_name="equipment_loans")
    op.drop_index("ix_equipment_loans_employee_requested", table_name="equipment_loans")
    op.drop_table("equipment_loans")
    op.drop_table("equipment")
    op.execute("DROP TYPE loan_status")
    op.execute("DROP TYPE equipment_status")
