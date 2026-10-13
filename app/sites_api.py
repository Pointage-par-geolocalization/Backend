from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .core import admin, audit, employee, fail, get_db, iso, settings
from .models import Attendance, AttendanceStatus, Employee, EmployeeWorkplace, User, Workplace
from .store import lock_domain, new_id, now_utc, remove, row, rows, save

router = APIRouter(tags=["sites"])

SITE_STATUSES = {"PREPARATION", "EN_COURS", "SUSPENDU", "TERMINE"}
TZ=ZoneInfo(settings.timezone_name)


def _body(value: dict[str, Any] | None) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _value(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return default


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


def _clean_text(value: Any, *, required: bool = False, message: str = "La valeur est obligatoire.", limit: int | None = None) -> str | None:
    if value is None:
        text = ""
    else:
        text = str(value).strip()
    if required and not text:
        fail(400, "VALIDATION_ERROR", message)
    if not text:
        return None
    return text[:limit] if limit else text


def _day(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        fail(400, "VALIDATION_ERROR", f"La date {field} est invalide.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        fail(400, "VALIDATION_ERROR", f"La date {field} est invalide.")
    return None


def _progress(value: Any) -> int:
    if isinstance(value, bool):
        fail(400, "VALIDATION_ERROR", "L'avancement est invalide.")
    try:
        result = int(value)
    except (TypeError, ValueError):
        fail(400, "VALIDATION_ERROR", "L'avancement est invalide.")
    if result < 0 or result > 100 or (isinstance(value, float) and value != result):
        fail(400, "VALIDATION_ERROR", "L'avancement doit être compris entre 0 et 100.")
    return result


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _clients(db: Session) -> list[dict[str, Any]]:
    return rows(db, "clients")


def _client(db: Session, client_id: Any) -> dict[str, Any] | None:
    wanted = _id(client_id)
    if not wanted:
        return None
    return next((c for c in _clients(db) if _id(_value(c, "id", "clientId")) == wanted), None)


def _workplace(db: Session, workplace_id: Any) -> Workplace | None:
    parsed = _uuid(workplace_id)
    return db.get(Workplace, parsed) if parsed else None


def _site_name(site: dict[str, Any]) -> str:
    return str(_value(site, "name", default=""))


def _site_rows(db: Session) -> list[dict[str, Any]]:
    return rows(db, "sites")


def _find_site(db: Session, site_id: str) -> dict[str, Any]:
    site = row(db, "sites", site_id)
    if not site:
        fail(404, "NOT_FOUND", "Chantier introuvable.")
    return site


def _employee_name(db: Session, code: str) -> tuple[str, Employee | None]:
    emp = db.get(Employee, code)
    return (f"{emp.first_name} {emp.last_name}" if emp else code, emp)


def _site_members(db: Session, site_id: str) -> list[dict[str, Any]]:
    result = []
    for member in rows(db, "site_members"):
        if _id(_value(member, "siteId", "site_id")) != site_id:
            continue
        code = str(_value(member, "employeeCode", "employee_code", default=""))
        name, emp = _employee_name(db, code)
        result.append({
            "employeeCode": code,
            "name": name,
            "position": emp.position if emp else None,
            "active": bool(emp.active) if emp else False,
        })
    return result


def _site_steps(db: Session, site_id: str) -> list[dict[str, Any]]:
    result = []
    for step in rows(db, "site_steps"):
        if _id(_value(step, "siteId", "site_id")) != site_id:
            continue
        result.append({
            "id": _id(_value(step, "id")),
            "title": str(_value(step, "title", default="")),
            "description": _value(step, "description", default=None),
            "position": int(_value(step, "position", default=0) or 0),
            "dueDate": _value(step, "dueDate", "due_date", default=None),
            "done": bool(_value(step, "done", default=False)),
            "doneAt": _value(step, "doneAt", "done_at", default=None),
        })
    return sorted(result, key=lambda item: (item["position"], item["id"] or ""))


def _site_notes(db: Session, site_id: str) -> list[dict[str, Any]]:
    result = []
    for note in rows(db, "site_notes"):
        if _id(_value(note, "siteId", "site_id")) != site_id:
            continue
        result.append({
            "id": _id(_value(note, "id")),
            "authorName": str(_value(note, "authorName", "author_name", default="")),
            "text": str(_value(note, "text", default="")),
            "createdAt": _value(note, "createdAt", "created_at", default=None),
        })
    return sorted(result, key=lambda item: item["createdAt"] or "", reverse=True)


def _site_dates(site: dict[str, Any]) -> tuple[str | None, str | None]:
    return (_value(site, "startDate", "start_date", default=None), _value(site, "endDate", "end_date", default=None))


def _site_is_active_for(site: dict[str, Any], workplace_id: Any, work_day: date) -> bool:
    if _id(_value(site, "workplaceId", "workplace_id")) != _id(workplace_id):
        return False
    if _value(site, "status", default="PREPARATION") == "TERMINE":
        return False
    start, end = _site_dates(site)
    key = work_day.isoformat()
    return (not start or key >= str(start)) and (not end or key <= str(end))


def _effective_site_id(db: Session, workplace_id: Any, when: datetime | None, explicit: Any) -> str | None:
    if explicit:
        return _id(explicit)
    if not when:
        return None
    local_day = when.astimezone(TZ).date()
    for candidate in _site_rows(db):
        if _site_is_active_for(candidate, workplace_id, local_day):
            return _id(_value(candidate, "id"))
    return None


def _attendance_for_site(db: Session, site: dict[str, Any]) -> list[Attendance]:
    wanted = _id(_value(site, "id"))
    result = []
    for item in db.scalars(select(Attendance)).all():
        explicit = _id(item.site_id)
        effective = _effective_site_id(db, item.workplace_id, item.entry_time, explicit)
        if effective == wanted:
            result.append(item)
    return result


def _document_rows(db: Session) -> list[dict[str, Any]]:
    return rows(db, "documents")


def _site_view(db: Session, site: dict[str, Any]) -> dict[str, Any]:
    site_id = _id(_value(site, "id")) or ""
    client_id = _id(_value(site, "clientId", "client_id"))
    workplace_id = _id(_value(site, "workplaceId", "workplace_id"))
    client = _client(db, client_id)
    workplace = _workplace(db, workplace_id)
    steps = _site_steps(db, site_id)
    done = sum(1 for step in steps if step["done"])
    manual = _progress(_value(site, "manualProgress", "manual_progress", default=0) or 0)
    return {
        "id": site_id,
        "name": _site_name(site),
        "clientId": client_id if client else None,
        "clientName": _value(client or {}, "name", default=_value(_value(site,"clientSnapshot","client_snapshot",default={}) or {},"name",default=None)),
        "workplaceId": _id(workplace.id) if workplace else None,
        "workplaceName": workplace.name if workplace else None,
        "address": _value(site, "address", default=None),
        "description": _value(site, "description", default=None),
        "startDate": _value(site, "startDate", "start_date", default=None),
        "endDate": _value(site, "endDate", "end_date", default=None),
        "status": _value(site, "status", default="PREPARATION"),
        "progress": round(done * 100 / len(steps)) if steps else manual,
        "manualProgress": manual,
        "memberCount": len([m for m in rows(db, "site_members") if _id(_value(m, "siteId", "site_id")) == site_id]),
        "stepsDone": done,
        "stepsTotal": len(steps),
    }


def _total(items: list[dict[str, Any]]) -> int:
    total = 0
    for item in items:
        try:
            total += int(float(_value(item, "total", "amount", default=0) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _stats(db: Session, site: dict[str, Any]) -> dict[str, int]:
    sessions = _attendance_for_site(db, site)
    documents = [d for d in _document_rows(db) if _id(_value(d, "siteId", "site_id")) == _id(_value(site, "id"))]
    leaves = [l for l in rows(db, "leaves") if _id(_value(l, "siteId", "site_id")) == _id(_value(site, "id"))]
    return {
        "attendanceCount": len(sessions),
        "totalMinutes": sum(int(a.worked_minutes or 0) for a in sessions),
        "totalSalary": sum(int(a.daily_salary or 0) for a in sessions),
        "presentNow": sum(1 for a in sessions if getattr(a.status, "value", a.status) == "EN_COURS"),
        "pendingLeaves": sum(1 for item in leaves if _value(item, "status", default=None) == "EN_ATTENTE"),
        "quotesTotal": _total([d for d in documents if _value(d, "type", default=None) == "DEVIS" and _value(d, "status", default=None) != "REFUSE"]),
        "invoicesTotal": _total([d for d in documents if _value(d, "type", default=None) == "FACTURE" and _value(d, "status", default=None) in {"EMISE", "PAYEE"}]),
        "invoicesPaid": _total([d for d in documents if _value(d, "type", default=None) == "FACTURE" and _value(d, "status", default=None) == "PAYEE"]),
    }


def _detail(db: Session, site: dict[str, Any]) -> dict[str, Any]:
    result = _site_view(db, site)
    result.update({"members": _site_members(db, _id(_value(site, "id")) or ""), "steps": _site_steps(db, _id(_value(site, "id")) or ""), "notes": _site_notes(db, _id(_value(site, "id")) or ""), "stats": _stats(db, site)})
    return result


def _site_fields(db: Session, body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    partial = current is not None
    result = dict(current or {})
    if "name" in body or not partial:
        result["name"] = _clean_text(body.get("name"), required=True, message="Le nom du chantier est obligatoire.")
    for field, persisted in (("clientId", "clientId"), ("workplaceId", "workplaceId")):
        if field in body or not partial:
            value = body.get(field)
            if field == "clientId":
                client=_client(db,value) if value else None
                if value and client is None: fail(404,"NOT_FOUND","Client introuvable.")
                result[persisted]=_id(client["id"]) if client else None
                if client:
                    result["clientSnapshot"]={key:client.get(key) for key in ("name","contactName","phone","email","address","nif","stat","notes")}
                elif not partial:
                    result["clientSnapshot"]=None
            else:
                workplace=_workplace(db,value) if value else None
                if value and workplace is None: fail(404,"NOT_FOUND","Lieu de travail introuvable.")
                result[persisted]=_id(workplace.id) if workplace else None
    for field in ("address", "description"):
        if field in body or not partial:
            result[field] = _clean_text(body.get(field), limit=10000)
    for field in ("startDate", "endDate"):
        if field in body or not partial:
            result[field] = _day(body.get(field), field)
    if "status" in body or not partial:
        status = body.get("status", "PREPARATION")
        if not isinstance(status,str) or status not in SITE_STATUSES:
            fail(400, "VALIDATION_ERROR", "Statut invalide.")
        result["status"] = status
    if "manualProgress" in body or not partial:
        result["manualProgress"] = _progress(body.get("manualProgress", 0))
    start, end = _value(result, "startDate", "start_date"), _value(result, "endDate", "end_date")
    if start and end and end < start:
        fail(400, "VALIDATION_ERROR", "La date de fin doit être après la date de début.")
    return result


def _save_all(db: Session, domain: str, values: list[dict[str, Any]]) -> None:
    for value in values:
        save(db, domain, value, _id(_value(value, "id")))


@router.get("/sites")
def list_sites(user: User = Depends(admin), db: Session = Depends(get_db)):
    return sorted((_site_view(db, site) for site in _site_rows(db)), key=lambda item: item["name"].casefold())


@router.get("/me/sites")
def list_my_sites(user: User = Depends(employee), db: Session = Depends(get_db)):
    code = user.employee_code
    site_ids = {_id(_value(member, "siteId", "site_id")) for member in rows(db, "site_members") if _value(member, "employeeCode", "employee_code") == code}
    return sorted((_site_view(db, site) for site in _site_rows(db) if _id(_value(site, "id")) in site_ids and _value(site, "status", default="PREPARATION") != "TERMINE"), key=lambda item: item["name"].casefold())


@router.get("/sites/{site_id}")
def get_site(site_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    return _detail(db, _find_site(db, site_id))


@router.post("/sites", status_code=201)
def create_site(body: dict[str, Any] = Body(default={}), user: User = Depends(admin), db: Session = Depends(get_db)):
    payload = _site_fields(db, _body(body))
    site_id = new_id()
    payload.update({"id": site_id, "createdAt": iso(now_utc())})
    save(db, "sites", payload, site_id)
    audit(db, user, "SITE_CREATED", site_id, f"Chantier « {payload['name']} » créé")
    db.commit()
    return {"id": site_id}


@router.patch("/sites/{site_id}", status_code=204)
def update_site(site_id: str, body: dict[str, Any] = Body(default={}), user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"sites",site_id)
    current = _find_site(db, site_id)
    payload = _site_fields(db, _body(body), current)
    payload["id"] = site_id
    save(db, "sites", payload, site_id)
    if "workplaceId" in body and body.get("workplaceId"):
        new_workplace=_workplace(db,body.get("workplaceId"))
        if new_workplace:
            member_codes={_value(item,"employeeCode","employee_code") for item in rows(db,"site_members") if _id(_value(item,"siteId","site_id"))==site_id}
            for code in member_codes:
                emp=db.get(Employee,code)
                if emp and not any(_id(w.id)==_id(new_workplace.id) for w in emp.workplaces): emp.workplaces.append(new_workplace)
    audit(db, user, "SITE_UPDATED", site_id, f"Chantier « {payload['name']} » modifié")
    db.commit()
    return Response(status_code=204)


@router.delete("/sites/{site_id}", status_code=204)
def delete_site(site_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"sites",site_id)
    site = _find_site(db, site_id)
    remove(db, "sites", site_id)
    for domain in ("site_members", "site_steps", "site_notes"):
        for item in rows(db, domain):
            if _id(_value(item, "siteId", "site_id")) == site_id:
                remove(db, domain, _id(_value(item, "id")) or "")
    # SQL attendance is detached; legacy leave/check records are retained and detached.
    parsed_site_id = _uuid(site_id)
    if parsed_site_id:
        db.execute(update(Attendance).where(Attendance.site_id == parsed_site_id).values(site_id=None))
    for domain in ("leaves", "checks", "documents"):
        values = rows(db, domain)
        changed = False
        for item in values:
            if _id(_value(item, "siteId", "site_id")) == site_id:
                if "site_id" in item:
                    item["site_id"] = None
                else:
                    item["siteId"] = None
                save(db, domain, item, _id(_value(item, "id")))
                changed = True
        if changed:
            db.flush()
    audit(db, user, "SITE_DELETED", site_id, f"Chantier « {_site_name(site)} » supprimé")
    db.commit()
    return Response(status_code=204)


@router.put("/sites/{site_id}/members", status_code=204)
def replace_members(site_id: str, body: dict[str, Any] = Body(default={}), user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"sites",site_id)
    site = _find_site(db, site_id)
    raw_codes = _body(body).get("employeeCodes", [])
    if not isinstance(raw_codes, list):
        fail(400, "VALIDATION_ERROR", "La liste des salariés est invalide.")
    codes = list(dict.fromkeys(str(code).strip() for code in raw_codes if str(code).strip()))
    employees: list[Employee] = []
    for code in codes:
        emp = db.get(Employee, code)
        if not emp:
            fail(404, "NOT_FOUND", "Salarié introuvable.")
        employees.append(emp)
    for item in rows(db, "site_members"):
        if _id(_value(item, "siteId", "site_id")) == site_id:
            remove(db, "site_members", _id(_value(item, "id")) or f"{site_id}:{_value(item, 'employeeCode', 'employee_code')}")
    workplace = _workplace(db, _value(site, "workplaceId", "workplace_id"))
    for emp in employees:
        save(db, "site_members", {"id": f"{site_id}:{emp.employee_code}", "siteId": site_id, "employeeCode": emp.employee_code}, f"{site_id}:{emp.employee_code}")
        if workplace and not any(_id(w.id) == _id(workplace.id) for w in emp.workplaces):
            emp.workplaces.append(workplace)
    audit(db, user, "SITE_UPDATED", site_id, f"Équipe du chantier « {_site_name(site)} » remplacée")
    db.commit()
    return Response(status_code=204)


@router.post("/sites/{site_id}/steps", status_code=204)
def add_step(site_id: str, body: dict[str, Any] = Body(default={}), user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"sites",site_id)
    _find_site(db, site_id)
    data = _body(body)
    title = _clean_text(data.get("title"), required=True, message="Le titre de l'étape est obligatoire.")
    due = _day(data.get("dueDate"), "dueDate")
    existing = [item for item in rows(db, "site_steps") if _id(_value(item, "siteId", "site_id")) == site_id]
    position = max((int(_value(item, "position", default=-1) or -1) for item in existing), default=-1) + 1
    step_id = new_id()
    save(db, "site_steps", {"id": step_id, "siteId": site_id, "title": title, "description": _clean_text(data.get("description"), limit=10000), "position": position, "dueDate": due, "done": False, "doneAt": None}, step_id)
    db.commit()
    return Response(status_code=204)


@router.patch("/sites/{site_id}/steps/{step_id}", status_code=204)
def update_step(site_id: str, step_id: str, body: dict[str, Any] = Body(default={}), user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"sites",site_id)
    _find_site(db, site_id)
    steps = rows(db, "site_steps")
    step = next((item for item in steps if _id(_value(item, "id")) == step_id and _id(_value(item, "siteId", "site_id")) == site_id), None)
    if not step:
        fail(404, "NOT_FOUND", "Étape introuvable.")
    data = _body(body)
    if "title" in data:
        step["title"] = _clean_text(data.get("title"), required=True, message="Le titre de l'étape est obligatoire.")
    if "description" in data:
        step["description"] = _clean_text(data.get("description"), limit=10000)
    if "dueDate" in data:
        step["dueDate"] = _day(data.get("dueDate"), "dueDate")
    if "position" in data:
        if isinstance(data["position"], bool):
            fail(400, "VALIDATION_ERROR", "La position est invalide.")
        try:
            step["position"] = int(data["position"])
        except (TypeError, ValueError):
            fail(400, "VALIDATION_ERROR", "La position est invalide.")
    if "done" in data:
        step["done"] = data["done"] is True
        step["doneAt"] = iso(now_utc()) if step["done"] else None
    save(db, "site_steps", step, step_id)
    db.commit()
    return Response(status_code=204)


@router.delete("/sites/{site_id}/steps/{step_id}", status_code=204)
def delete_step(site_id: str, step_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"sites",site_id)
    _find_site(db, site_id)
    step = next((item for item in rows(db, "site_steps") if _id(_value(item, "id")) == step_id and _id(_value(item, "siteId", "site_id")) == site_id), None)
    if not step:
        fail(404, "NOT_FOUND", "Étape introuvable.")
    remove(db, "site_steps", step_id)
    db.commit()
    return Response(status_code=204)


@router.post("/sites/{site_id}/notes", status_code=204)
def add_note(site_id: str, body: dict[str, Any] = Body(default={}), user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"sites",site_id)
    _find_site(db, site_id)
    text = _clean_text(_body(body).get("text"), required=True, message="La note est vide.", limit=4000)
    note_id = new_id()
    save(db, "site_notes", {"id": note_id, "siteId": site_id, "authorName": _user_name(db, user), "text": text, "createdAt": iso(now_utc())}, note_id)
    db.commit()
    return Response(status_code=204)


def _user_name(db: Session, user: User) -> str:
    if user.employee_code:
        emp = db.get(Employee, user.employee_code)
        if emp:
            return f"{emp.first_name} {emp.last_name}"
    return "Administrateur"


@router.delete("/sites/{site_id}/notes/{note_id}", status_code=204)
def delete_note(site_id: str, note_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"sites",site_id)
    _find_site(db, site_id)
    note = next((item for item in rows(db, "site_notes") if _id(_value(item, "id")) == note_id and _id(_value(item, "siteId", "site_id")) == site_id), None)
    if not note:
        fail(404, "NOT_FOUND", "Note introuvable.")
    remove(db, "site_notes", note_id)
    db.commit()
    return Response(status_code=204)
