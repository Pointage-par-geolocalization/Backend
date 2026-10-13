from __future__ import annotations

from datetime import date, timedelta
from math import ceil, isfinite
import re
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import store
from .core import admin, audit, employee, fail, get_db, user_payload
from .models import Employee, Role, User, utcnow


router = APIRouter(tags=["leaves"])

LEAVE_TYPES = {"CONGE", "MALADIE", "FAMILLE", "ADMINISTRATIF", "AUTRE"}
LEAVE_UNITS = {"JOURS", "HEURES"}
LEAVE_STATUSES = {"EN_ATTENTE", "ACCEPTEE", "REFUSEE", "ANNULEE"}
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validation(message: str, details: Any = None) -> None:
    fail(400, "VALIDATION_ERROR", message, details)


def _day(value: Any, label: str = "date") -> date:
    if not isinstance(value, str) or not _DAY_RE.fullmatch(value):
        _validation(f"La {label} doit être au format YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _validation(f"La {label} doit être une date valide au format YYYY-MM-DD.")
    raise AssertionError("unreachable")


def _duration(value: Any, unit: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _validation("La durée doit être un nombre.")
    number = float(value)
    maximum = 365.0 if unit == "JOURS" else 24.0
    if not isfinite(number) or number < 0.25 or number > maximum:
        _validation(f"La durée doit être comprise entre 0,25 et {int(maximum)}.")
    return int(number) if number.is_integer() else number


def _create_fields(body: dict[str, Any]) -> tuple[str, str, date, int | float, str, str, str | None]:
    leave_type = body.get("type")
    if leave_type not in LEAVE_TYPES:
        _validation("Type de permission invalide.")

    unit = body.get("durationUnit")
    if unit not in LEAVE_UNITS:
        _validation("Unité de durée invalide.")

    start = _day(body.get("startDate"), "date de début")
    duration = _duration(body.get("durationValue"), unit)

    reason_value = body.get("reason")
    if not isinstance(reason_value, str):
        _validation("Le motif est obligatoire.")
    reason = reason_value.strip()
    if not reason:
        _validation("Le motif est obligatoire.")
    if len(reason) > 1000:
        _validation("Le motif ne peut pas dépasser 1000 caractères.")

    site_id = body.get("siteId")
    if site_id is not None and not isinstance(site_id, str):
        _validation("Le chantier est invalide.")
    if site_id == "":
        site_id = None

    return leave_type, unit, start, duration, reason, start.isoformat(), site_id


def _site(db: Session, site_id: str | None) -> dict[str, Any] | None:
    if not site_id:
        return None
    return store.row(db, "sites", site_id)


def _employee_name(db: Session, employee_code: str) -> str:
    person = db.get(Employee, employee_code)
    return f"{person.first_name} {person.last_name}" if person else employee_code


def _view(db: Session, leave: dict[str, Any]) -> dict[str, Any]:
    result = dict(leave)
    result["employeeName"] = _employee_name(db, str(leave["employeeCode"]))
    site = _site(db, leave.get("siteId"))
    result["siteName"] = site.get("name") if site else None
    # Keep the response contract explicit and camelCase even if old records contain extras.
    return {
        "id": str(result["id"]),
        "employeeCode": result["employeeCode"],
        "employeeName": result["employeeName"],
        "type": result["type"],
        "startDate": result["startDate"],
        "endDate": result["endDate"],
        "durationValue": result["durationValue"],
        "durationUnit": result["durationUnit"],
        "reason": result["reason"],
        "siteId": result.get("siteId"),
        "siteName": result["siteName"],
        "status": result["status"],
        "decidedAt": result.get("decidedAt"),
        "decidedByName": result.get("decidedByName"),
        "decisionNote": result.get("decisionNote"),
        "employeeSeenAt": result.get("employeeSeenAt"),
        "createdAt": result["createdAt"],
    }


def _end_date(start: date, duration: int | float, unit: str) -> str:
    if unit == "HEURES":
        return start.isoformat()
    return (start + timedelta(days=max(1, ceil(float(duration))) - 1)).isoformat()


def _leaves(db: Session) -> list[dict[str, Any]]:
    return store.rows(db, "leaves")


def _find(db: Session, leave_id: str) -> dict[str, Any] | None:
    return store.row(db, "leaves", leave_id)


def _admin_recipients(db: Session, employee_code: str) -> set[str]:
    recipients = {
        str(user.id)
        for user in db.scalars(
            select(User).where(User.role == Role.admin, User.active.is_(True))
        )
    }
    owner = db.scalar(
        select(User).where(
            User.role == Role.employee,
            User.employee_code == employee_code,
            User.active.is_(True),
        )
    )
    if owner:
        recipients.add(str(owner.id))
    return recipients


def _notify(db: Session, leave: dict[str, Any]) -> None:
    # The event queue is optional in this first backend; keep REST mutations usable when
    # websocket delivery is not installed, while using the shared event contract when it is.
    try:
        store.emit_after_commit(
            db,
            {"type": "leave.updated", "leaveId": str(leave["id"])},
            _admin_recipients(db, str(leave["employeeCode"])),
        )
    except (ImportError, AttributeError):
        pass


def _note(body: dict[str, Any]) -> str | None:
    value = body.get("note")
    if value is None:
        return None
    if not isinstance(value, str):
        _validation("La note de décision est invalide.")
    value = value.strip()
    return value or None


@router.get("/me/leaves")
def my_leaves(user: User = Depends(employee), db: Session = Depends(get_db)):
    rows = [leave for leave in _leaves(db) if leave.get("employeeCode") == user.employee_code]
    rows.sort(key=lambda leave: str(leave.get("createdAt", "")), reverse=True)
    return [_view(db, leave) for leave in rows]


@router.post("/leaves", status_code=201)
def create_leave(data: dict[str, Any], user: User = Depends(employee), db: Session = Depends(get_db)):
    leave_type, unit, start, duration, reason, start_key, site_id = _create_fields(data)
    if site_id and _site(db, site_id) is None:
        fail(400, "VALIDATION_ERROR", "Chantier introuvable.")

    store.lock_domain(db, "leaves")
    created = store.save(
        db,
        "leaves",
        {
            "employeeCode": user.employee_code,
            "type": leave_type,
            "startDate": start_key,
            "endDate": _end_date(start, duration, unit),
            "durationValue": duration,
            "durationUnit": unit,
            "reason": reason,
            "siteId": site_id,
            "status": "EN_ATTENTE",
            "decidedAt": None,
            "decidedByName": None,
            "decisionNote": None,
            "employeeSeenAt": None,
            "createdAt": store.iso(utcnow()),
        },
    )
    db.commit()
    _notify(db, created)
    return _view(db, created)


@router.post("/leaves/{leave_id}/cancel", status_code=204)
def cancel_leave(leave_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
    store.lock_domain(db, "leaves", leave_id)
    leave = _find(db, leave_id)
    # A foreign request is intentionally indistinguishable from a missing request.
    if leave is None or leave.get("employeeCode") != user.employee_code:
        fail(404, "NOT_FOUND", "Demande introuvable.")
    if leave.get("status") != "EN_ATTENTE":
        fail(409, "LEAVE_NOT_PENDING", "Cette demande a déjà été traitée.")

    leave["status"] = "ANNULEE"
    updated = store.save(db, "leaves", leave, key=leave_id)
    db.commit()
    _notify(db, updated)


@router.post("/me/leaves/seen", status_code=204)
def mark_leaves_seen(user: User = Depends(employee), db: Session = Depends(get_db)):
    store.lock_domain(db, "leaves", user.employee_code or "seen")
    now = store.iso(utcnow())
    changed: list[dict[str, Any]] = []
    for leave in _leaves(db):
        if (
            leave.get("employeeCode") == user.employee_code
            and leave.get("status") in {"ACCEPTEE", "REFUSEE"}
            and not leave.get("employeeSeenAt")
        ):
            leave["employeeSeenAt"] = now
            changed.append(store.save(db, "leaves", leave, key=str(leave["id"])))
    db.commit()
    for leave in changed:
        _notify(db, leave)


@router.get("/leaves")
def list_leaves(
    status: str | None = None,
    employeeCode: str | None = None,
    siteId: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to_: str | None = Query(default=None, alias="to"),
    user: User = Depends(admin),
    db: Session = Depends(get_db),
):
    if status is not None and status not in LEAVE_STATUSES:
        _validation("Statut de permission invalide.")
    from_date = _day(from_, "la date de début du filtre") if from_ else None
    to_date = _day(to_, "la date de fin du filtre") if to_ else None
    if from_date and to_date and from_date > to_date:
        _validation("L'intervalle de dates est invalide.")

    result = []
    for leave in _leaves(db):
        if status and leave.get("status") != status:
            continue
        if employeeCode and leave.get("employeeCode") != employeeCode:
            continue
        if siteId and leave.get("siteId") != siteId:
            continue
        # Inclusive overlap: [startDate, endDate] intersects [from, to].
        if from_date and _day(leave.get("endDate"), "la date de fin") < from_date:
            continue
        if to_date and _day(leave.get("startDate"), "la date de début") > to_date:
            continue
        result.append(leave)
    result.sort(key=lambda leave: str(leave.get("createdAt", "")), reverse=True)
    return [_view(db, leave) for leave in result]


@router.get("/leaves/pending-count")
def pending_count(user: User = Depends(admin), db: Session = Depends(get_db)):
    return {"count": sum(1 for leave in _leaves(db) if leave.get("status") == "EN_ATTENTE")}


def _decide(
    leave_id: str,
    status: str,
    data: dict[str, Any],
    user: User,
    db: Session,
) -> None:
    store.lock_domain(db, "leaves", leave_id)
    leave = _find(db, leave_id)
    if leave is None:
        fail(404, "NOT_FOUND", "Demande introuvable.")
    if leave.get("status") != "EN_ATTENTE":
        fail(409, "LEAVE_NOT_PENDING", "Cette demande a déjà été traitée.")

    decided_at = store.iso(utcnow())
    leave.update(
        {
            "status": status,
            "decidedAt": decided_at,
            "decidedByName": user_payload(user, db).get("name") or "Administrateur",
            "decisionNote": _note(data),
            "employeeSeenAt": None,
        }
    )
    updated = store.save(db, "leaves", leave, key=leave_id)
    action = "LEAVE_APPROVED" if status == "ACCEPTEE" else "LEAVE_REFUSED"
    verb = "acceptée" if status == "ACCEPTEE" else "refusée"
    audit(
        db,
        user,
        action,
        leave_id,
        f"Permission {verb} pour {_employee_name(db, str(leave['employeeCode']))} ({leave['type']})",
    )
    db.commit()
    _notify(db, updated)


@router.post("/leaves/{leave_id}/approve", status_code=204)
def approve_leave(
    leave_id: str,
    data: dict[str, Any] | None = None,
    user: User = Depends(admin),
    db: Session = Depends(get_db),
):
    _decide(leave_id, "ACCEPTEE", data or {}, user, db)


@router.post("/leaves/{leave_id}/refuse", status_code=204)
def refuse_leave(
    leave_id: str,
    data: dict[str, Any] | None = None,
    user: User = Depends(admin),
    db: Session = Depends(get_db),
):
    _decide(leave_id, "REFUSEE", data or {}, user, db)
