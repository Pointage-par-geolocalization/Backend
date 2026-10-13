from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .core import admin, current_user, employee, fail, get_db, settings
from .models import (
    AuditLog,
    Employee,
    Equipment,
    EquipmentLoan,
    EquipmentStatus,
    LoanStatus,
    Role,
    User,
)
from .store import emit_after_commit, iso, lock_domain, new_id, now_utc

router = APIRouter(tags=["equipment"])
ACTIVE_LOAN_STATUSES = {"EN_COURS", "RETOUR_DEMANDE"}
PENDING_LOAN_STATUSES = {"DEMANDE", "RETOUR_DEMANDE"}
TZ = ZoneInfo(settings.timezone_name)


class EquipmentCreate(BaseModel):
    name: str
    category: str | None = None
    serialNumber: str | None = None
    description: str | None = None


class EquipmentPatch(BaseModel):
    name: str | None = None
    category: str | None = None
    serialNumber: str | None = None
    description: str | None = None


class OutOfServiceIn(BaseModel):
    outOfService: bool


class LoanCreate(BaseModel):
    equipmentId: str
    note: str | None = None
    expectedReturnDate: str | None = None


class ReturnRequestIn(BaseModel):
    note: str | None = None


class RefuseLoanIn(BaseModel):
    reason: str | None = None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _equipment_fields(data: EquipmentCreate | EquipmentPatch, *, partial: bool) -> dict[str, Any]:
    name = _clean(data.name)
    if not partial and not name:
        fail(400, "VALIDATION_ERROR", "Le nom du matériel est obligatoire.")
    if partial and data.name is not None and not name:
        fail(400, "VALIDATION_ERROR", "Le nom du matériel est obligatoire.")
    result: dict[str, Any] = {}
    if not partial or data.name is not None:
        result["name"] = name
    for source, target in (("category", "category"), ("serialNumber", "serial_number"), ("description", "description")):
        value = getattr(data, source)
        if not partial or value is not None:
            result[target] = _clean(value)
    return result


def _not_found(message: str) -> None:
    fail(404, "NOT_FOUND", message)


def _uuid(value: Any) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _equipment_model(db: Session, equipment_id: str) -> Equipment:
    parsed = _uuid(equipment_id)
    equipment = db.get(Equipment, parsed) if parsed else None
    if equipment is None:
        _not_found("Ce matériel n'existe plus.")
    return equipment


def _equipment_dict(item: Equipment) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "category": item.category,
        "serialNumber": item.serial_number,
        "description": item.description,
        "status": item.status.value,
    }


def _equipment(db: Session, equipment_id: str) -> dict[str, Any]:
    return _equipment_dict(_equipment_model(db, equipment_id))


def _equipment_rows(db: Session) -> list[dict[str, Any]]:
    return [_equipment_dict(item) for item in db.scalars(select(Equipment)).all()]


def _save_equipment(db: Session, payload: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    equipment_id = _uuid(key or payload.get("id"))
    if equipment_id is None:
        fail(400, "VALIDATION_ERROR", "Identifiant matériel invalide.")
    item = db.get(Equipment, equipment_id)
    if item is None:
        item = Equipment(id=equipment_id, name=payload.get("name") or "")
        db.add(item)
    for field in ("name", "category", "serial_number", "description"):
        if field in payload:
            setattr(item, field, payload[field])
    if "status" in payload:
        item.status = EquipmentStatus(payload["status"])
    db.flush()
    return _equipment_dict(item)


def _loan_model(db: Session, loan_id: str, *, lock: bool = False) -> EquipmentLoan:
    parsed = _uuid(loan_id)
    stmt = select(EquipmentLoan).where(EquipmentLoan.id == parsed) if parsed else None
    loan = db.scalar(stmt.with_for_update()) if lock and stmt is not None else (db.scalar(stmt) if stmt is not None else None)
    if loan is None:
        _not_found("Prêt introuvable.")
    return loan


def _loan_dict(item: EquipmentLoan) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "equipmentId": str(item.equipment_id) if item.equipment_id else None,
        "equipmentName": item.equipment_name,
        "employeeCode": item.employee_code,
        "employeeName": item.employee_name,
        "status": item.status.value,
        "requestNote": item.request_note,
        "expectedReturnDate": item.expected_return_date.isoformat() if item.expected_return_date else None,
        "requestedAt": iso(item.requested_at),
        "decidedAt": iso(item.decided_at) if item.decided_at else None,
        "refusalReason": item.refusal_reason,
        "loanedAt": iso(item.loaned_at) if item.loaned_at else None,
        "returnRequestedAt": iso(item.return_requested_at) if item.return_requested_at else None,
        "returnNote": item.return_note,
        "returnedAt": iso(item.returned_at) if item.returned_at else None,
    }


def _loan(db: Session, loan_id: str, *, lock: bool = False) -> dict[str, Any]:
    return _loan_dict(_loan_model(db, loan_id, lock=lock))


def _loan_rows(db: Session) -> list[dict[str, Any]]:
    return [_loan_dict(item) for item in db.scalars(select(EquipmentLoan)).all()]


def _save_loan(db: Session, payload: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    loan_id = _uuid(key or payload.get("id"))
    if loan_id is None:
        fail(400, "VALIDATION_ERROR", "Identifiant de prêt invalide.")
    item = db.get(EquipmentLoan, loan_id)
    if item is None:
        equipment_id = _uuid(payload.get("equipmentId"))
        item = EquipmentLoan(
            id=loan_id,
            equipment_id=equipment_id,
            equipment_name=payload.get("equipmentName") or "",
            employee_code=payload.get("employeeCode"),
            employee_name=payload.get("employeeName") or "",
        )
        db.add(item)
    item.equipment_id = _uuid(payload.get("equipmentId")) if payload.get("equipmentId") else None
    item.equipment_name = str(payload.get("equipmentName") or item.equipment_name or "")
    item.employee_code = payload.get("employeeCode")
    item.employee_name = str(payload.get("employeeName") or item.employee_name or "")
    item.status = LoanStatus(payload.get("status") or (item.status.value if item.status else "DEMANDE"))
    item.request_note = payload.get("requestNote")
    expected = payload.get("expectedReturnDate")
    if expected:
        try:
            item.expected_return_date = date.fromisoformat(str(expected))
        except ValueError:
            fail(400, "VALIDATION_ERROR", "La date de retour souhaitée est invalide.")
    else:
        item.expected_return_date = None
    for source, target in (
        ("requestedAt", "requested_at"),
        ("decidedAt", "decided_at"),
        ("loanedAt", "loaned_at"),
        ("returnRequestedAt", "return_requested_at"),
        ("returnedAt", "returned_at"),
    ):
        if source in payload:
            parsed = _timestamp(payload.get(source))
            if parsed is not None:
                setattr(item, target, parsed)
    item.refusal_reason = payload.get("refusalReason")
    item.return_note = payload.get("returnNote")
    if payload.get("decidedBy"):
        item.decided_by = _uuid(payload["decidedBy"])
    if payload.get("confirmedBy"):
        item.confirmed_by = _uuid(payload["confirmedBy"])
    db.flush()
    return _loan_dict(item)


def _employee(db: Session, employee_code: str | None) -> Employee | None:
    return db.get(Employee, employee_code) if employee_code else None


def _employee_name(db: Session, employee_code: str | None) -> str:
    emp = _employee(db, employee_code)
    return f"{emp.first_name} {emp.last_name}" if emp else (employee_code or "Salarié")


def _user_ids(db: Session, *, employee_code: str | None = None, include_admins: bool = True) -> set[str]:
    recipients: set[str] = set()
    if include_admins:
        recipients.update(
            str(user.id)
            for user in db.scalars(select(User).where(User.role == Role.admin, User.active.is_(True)))
        )
    if employee_code:
        user = db.scalar(
            select(User).where(
                User.role == Role.employee,
                User.employee_code == employee_code,
                User.active.is_(True),
            )
        )
        if user:
            recipients.add(str(user.id))
    return recipients


def _emit(db: Session, event_type: str, *, employee_code: str | None = None, admins: bool = True) -> None:
    recipients = _user_ids(db, employee_code=employee_code, include_admins=admins)
    if event_type == "equipment.updated":
        recipients.update(str(user.id) for user in db.scalars(select(User).where(User.role == Role.employee, User.active.is_(True))))
    emit_after_commit(db, {"type": event_type}, recipients or None)


def _audit(db: Session, user: User, action: str, target_id: str | None, summary: str) -> None:
    label = "Administrateur" if user.role == Role.admin else _employee_name(db, user.employee_code)
    db.add(AuditLog(user_id=user.id, user_label=label, action=action, target_id=str(target_id) if target_id else None, summary=summary))


def _equipment_view(db: Session, equipment: dict[str, Any], loans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    loans = loans if loans is not None else _loan_rows(db)
    current = next(
        (loan for loan in loans if loan.get("equipmentId") == equipment["id"] and loan.get("status") in ACTIVE_LOAN_STATUSES),
        None,
    )
    return {
        **equipment,
        "currentLoanId": current.get("id") if current else None,
        "currentBorrowerCode": current.get("employeeCode") if current else None,
        "currentBorrowerName": current.get("employeeName") if current else None,
        "currentLoanedAt": current.get("loanedAt") if current else None,
    }


def _ordered_loans(db: Session, *, employee_code: str | None = None, status: str | None = None, equipment_id: str | None = None) -> list[dict[str, Any]]:
    result = _loan_rows(db)
    if employee_code is not None:
        result = [loan for loan in result if loan.get("employeeCode") == employee_code]
    if status is not None:
        result = [loan for loan in result if loan.get("status") == status]
    if equipment_id is not None:
        result = [loan for loan in result if loan.get("equipmentId") == equipment_id]
    return sorted(result, key=lambda loan: loan.get("requestedAt") or "", reverse=True)


def _expected_date(value: str | None) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        fail(400, "VALIDATION_ERROR", "La date de retour souhaitée est invalide.")
    if parsed < datetime.now(TZ).date():
        fail(400, "VALIDATION_ERROR", "La date de retour souhaitée est déjà passée.")
    return parsed.isoformat()


@router.get("/equipment")
def list_equipment(user: User = Depends(current_user), db: Session = Depends(get_db)):
    equipment = sorted(_equipment_rows(db), key=lambda item: (item.get("name") or "").casefold())
    loans = _loan_rows(db)
    return [_equipment_view(db, item, loans) for item in equipment]


@router.post("/equipment", status_code=201)
def create_equipment(data: EquipmentCreate, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db, "equipment")
    equipment = _save_equipment(db, {"id": new_id(), **_equipment_fields(data, partial=False), "status": "DISPONIBLE"})
    _audit(db, user, "EQUIPMENT_CREATED", equipment["id"], f"Matériel « {equipment['name']} » créé")
    db.commit()
    _emit(db, "equipment.updated")
    return _equipment_view(db, equipment)


@router.patch("/equipment/{equipment_id}")
def update_equipment(equipment_id: str, data: EquipmentPatch, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db, "equipment", equipment_id)
    equipment = _equipment(db, equipment_id)
    equipment.update(_equipment_fields(data, partial=True))
    equipment = _save_equipment(db, equipment, equipment_id)
    _audit(db, user, "EQUIPMENT_UPDATED", equipment_id, f"Matériel « {equipment['name']} » modifié")
    db.commit()
    _emit(db, "equipment.updated")
    return _equipment_view(db, equipment)


@router.patch("/equipment/{equipment_id}/out-of-service")
def set_out_of_service(equipment_id: str, data: OutOfServiceIn, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db, "equipment", equipment_id)
    equipment = _equipment(db, equipment_id)
    active_loan = next((loan for loan in _loan_rows(db) if loan.get("equipmentId") == equipment_id and loan.get("status") in ACTIVE_LOAN_STATUSES), None)
    if equipment.get("status") == "EMPRUNTE" or active_loan:
        fail(409, "EQUIPMENT_BORROWED", "Ce matériel est actuellement emprunté.")
    equipment["status"] = "HORS_SERVICE" if data.outOfService else "DISPONIBLE"
    equipment = _save_equipment(db, equipment, equipment_id)
    _audit(db, user, "EQUIPMENT_OUT_OF_SERVICE" if data.outOfService else "EQUIPMENT_AVAILABLE", equipment_id, f"Statut du matériel « {equipment['name']} » modifié")
    db.commit()
    _emit(db, "equipment.updated")
    return _equipment_view(db, equipment)


@router.delete("/equipment/{equipment_id}", status_code=204)
def delete_equipment(equipment_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db, "equipment", equipment_id)
    item = _equipment_model(db, equipment_id)
    equipment = _equipment_dict(item)
    loans = _loan_rows(db)
    if equipment.get("status") == "EMPRUNTE" or any(loan.get("equipmentId") == equipment_id and loan.get("status") in ACTIVE_LOAN_STATUSES for loan in loans):
        fail(409, "EQUIPMENT_BORROWED", "Impossible de supprimer un matériel actuellement emprunté.")
    affected_codes = {loan["employeeCode"] for loan in loans if loan.get("equipmentId") == equipment_id and loan.get("employeeCode")}
    db.execute(update(EquipmentLoan).where(EquipmentLoan.equipment_id == item.id).values(equipment_id=None))
    db.delete(item)
    _audit(db, user, "EQUIPMENT_DELETED", equipment_id, f"Matériel « {equipment['name']} » supprimé")
    db.commit()
    recipients = _user_ids(db)
    for code in affected_codes:
        recipients.update(_user_ids(db, employee_code=code, include_admins=False))
    emit_after_commit(db, {"type": "equipment.updated"}, recipients or None)


@router.get("/loans")
def list_loans(status: str | None = Query(default=None), equipmentId: str | None = None, user: User = Depends(admin), db: Session = Depends(get_db)):
    return _ordered_loans(db, status=status, equipment_id=equipmentId)


@router.get("/me/loans")
def list_my_loans(user: User = Depends(employee), db: Session = Depends(get_db)):
    return _ordered_loans(db, employee_code=user.employee_code)


@router.get("/loans/pending-count")
def pending_count(user: User = Depends(admin), db: Session = Depends(get_db)):
    count = db.scalar(select(func.count()).select_from(EquipmentLoan).where(EquipmentLoan.status.in_([LoanStatus.DEMANDE, LoanStatus.RETOUR_DEMANDE]))) or 0
    return {"count": count}


@router.post("/loans", status_code=201)
def request_loan(data: LoanCreate, user: User = Depends(employee), db: Session = Depends(get_db)):
    equipment_id = _clean(data.equipmentId)
    if not equipment_id:
        fail(400, "VALIDATION_ERROR", "Le matériel est obligatoire.")
    lock_domain(db, "equipment", equipment_id)
    equipment = _equipment(db, equipment_id)
    if equipment.get("status") != "DISPONIBLE":
        fail(409, "EQUIPMENT_UNAVAILABLE", "Ce matériel n'est pas disponible.")
    if db.scalar(select(EquipmentLoan.id).where(EquipmentLoan.equipment_id == _uuid(equipment_id), EquipmentLoan.employee_code == user.employee_code, EquipmentLoan.status == LoanStatus.DEMANDE).limit(1)):
        fail(409, "LOAN_ALREADY_PENDING", "Vous avez déjà une demande en attente pour ce matériel.")
    emp = _employee(db, user.employee_code)
    if not emp:
        _not_found("Salarié introuvable.")
    loan = _save_loan(db, {
        "id": new_id(), "equipmentId": equipment_id, "equipmentName": equipment.get("name", ""),
        "employeeCode": emp.employee_code, "employeeName": f"{emp.first_name} {emp.last_name}",
        "status": "DEMANDE", "requestNote": _clean(data.note), "expectedReturnDate": _expected_date(data.expectedReturnDate),
        "requestedAt": iso(), "decidedAt": None, "refusalReason": None, "loanedAt": None,
        "returnRequestedAt": None, "returnNote": None, "returnedAt": None,
    })
    db.commit()
    _emit(db, "loan.updated", employee_code=user.employee_code)
    _emit(db, "loans.pending")
    return loan


@router.post("/loans/{loan_id}/cancel", status_code=204)
def cancel_loan(loan_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
    lock_domain(db, "loans", loan_id)
    loan = _loan(db, loan_id, lock=True)
    if loan.get("employeeCode") != user.employee_code:
        fail(403, "FORBIDDEN", "Accès refusé.")
    if loan.get("status") != "DEMANDE":
        fail(409, "LOAN_NOT_PENDING", "Cette demande a déjà été traitée.")
    loan.update({"status": "ANNULEE", "decidedAt": iso()})
    _save_loan(db, loan, loan_id)
    db.commit()
    _emit(db, "loan.updated", employee_code=user.employee_code)
    _emit(db, "loans.pending")


@router.post("/loans/{loan_id}/return-request")
def request_return(loan_id: str, data: ReturnRequestIn | None = None, user: User = Depends(employee), db: Session = Depends(get_db)):
    lock_domain(db, "loans", loan_id)
    loan = _loan(db, loan_id, lock=True)
    if loan.get("employeeCode") != user.employee_code:
        fail(403, "FORBIDDEN", "Accès refusé.")
    if loan.get("status") != "EN_COURS":
        fail(409, "LOAN_NOT_ACTIVE", "Ce prêt n'est plus en cours.")
    loan.update({"status": "RETOUR_DEMANDE", "returnRequestedAt": iso(), "returnNote": _clean(data.note) if data else None})
    result = _save_loan(db, loan, loan_id)
    db.commit()
    _emit(db, "loan.updated", employee_code=user.employee_code)
    _emit(db, "loans.pending")
    return result


@router.post("/loans/{loan_id}/approve")
def approve_loan(loan_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    preview = _loan(db, loan_id)
    equipment_id = preview.get("equipmentId")
    if equipment_id:
        lock_domain(db, "equipment", equipment_id)
    lock_domain(db, "loans", loan_id)
    loan = _loan(db, loan_id, lock=True)
    if loan.get("equipmentId") != equipment_id:
        fail(409, "EQUIPMENT_UNAVAILABLE", "Le matériel associé à ce prêt a été supprimé ou modifié.")
    if loan.get("status") != "DEMANDE":
        fail(409, "LOAN_NOT_PENDING", "Cette demande a déjà été traitée.")
    if not equipment_id:
        _not_found("Ce matériel n'existe plus.")
    equipment = _equipment(db, equipment_id)
    active_elsewhere = db.scalar(select(EquipmentLoan.id).where(EquipmentLoan.equipment_id == _uuid(equipment_id), EquipmentLoan.id != _uuid(loan_id), EquipmentLoan.status.in_([LoanStatus.EN_COURS, LoanStatus.RETOUR_DEMANDE])).limit(1))
    if equipment.get("status") == "EMPRUNTE" or active_elsewhere:
        fail(409, "EQUIPMENT_BORROWED", "Ce matériel est déjà emprunté par quelqu'un d'autre.")
    if equipment.get("status") == "HORS_SERVICE":
        fail(409, "EQUIPMENT_UNAVAILABLE", "Ce matériel est hors service.")
    timestamp = iso()
    loan.update({"status": "EN_COURS", "loanedAt": timestamp, "decidedAt": timestamp, "decidedBy": str(user.id)})
    equipment["status"] = "EMPRUNTE"
    _save_loan(db, loan, loan_id)
    _save_equipment(db, equipment, equipment_id)
    _audit(db, user, "LOAN_APPROVED", loan_id, f"« {loan['equipmentName']} » prêté à {loan['employeeName']}")
    db.commit()
    _emit(db, "loan.updated", employee_code=loan.get("employeeCode"))
    _emit(db, "equipment.updated", employee_code=loan.get("employeeCode"))
    _emit(db, "loans.pending")
    return _loan(db, loan_id)


@router.post("/loans/{loan_id}/refuse")
def refuse_loan(loan_id: str, data: RefuseLoanIn | None = None, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db, "loans", loan_id)
    loan = _loan(db, loan_id, lock=True)
    if loan.get("status") != "DEMANDE":
        fail(409, "LOAN_NOT_PENDING", "Cette demande a déjà été traitée.")
    loan.update({"status": "REFUSEE", "refusalReason": _clean(data.reason) if data else None, "decidedAt": iso(), "decidedBy": str(user.id)})
    result = _save_loan(db, loan, loan_id)
    _audit(db, user, "LOAN_REFUSED", loan_id, f"Demande de « {loan['equipmentName']} » refusée pour {loan['employeeName']}")
    db.commit()
    _emit(db, "loan.updated", employee_code=loan.get("employeeCode"))
    _emit(db, "loans.pending")
    return result


@router.post("/loans/{loan_id}/confirm-return")
def confirm_return(loan_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    preview = _loan(db, loan_id)
    equipment_id = preview.get("equipmentId")
    if equipment_id:
        lock_domain(db, "equipment", equipment_id)
    lock_domain(db, "loans", loan_id)
    loan = _loan(db, loan_id, lock=True)
    if loan.get("equipmentId") != equipment_id:
        fail(409, "EQUIPMENT_UNAVAILABLE", "Le matériel associé à ce prêt a été supprimé ou modifié.")
    if loan.get("status") not in ACTIVE_LOAN_STATUSES:
        fail(409, "LOAN_NOT_ACTIVE", "Ce prêt n'est plus en cours.")
    loan.update({"status": "RETOURNE", "returnedAt": iso(), "confirmedBy": str(user.id)})
    result = _save_loan(db, loan, loan_id)
    if equipment_id:
        equipment = _equipment(db, equipment_id)
        if equipment.get("status") == "EMPRUNTE":
            equipment["status"] = "DISPONIBLE"
            _save_equipment(db, equipment, equipment_id)
    _audit(db, user, "LOAN_RETURNED", loan_id, f"Retour de « {loan['equipmentName']} » confirmé ({loan['employeeName']})")
    db.commit()
    _emit(db, "loan.updated", employee_code=loan.get("employeeCode"))
    _emit(db, "equipment.updated", employee_code=loan.get("employeeCode"))
    _emit(db, "loans.pending")
    return result
