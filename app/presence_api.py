from __future__ import annotations

"""Presence controls.

Rounds and checks deliberately live in :mod:`app.store` rather than in new ORM
models.  This keeps this feature additive to the existing schema while still
making every mutation transactional through the request's SQLAlchemy session.
"""

import os
from datetime import date, datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import store
from .core import admin, audit, current_user, employee, fail, get_db, iso, settings, tenant_singleton
from .models import AppSettings, Attendance, AttendanceStatus, Employee, Role, User, Workplace, utcnow


router = APIRouter(tags=["presence"])
TZ = ZoneInfo(settings.timezone_name)
TRACKING_DOMAIN = "attendance_tracking"
ROUNDS_DOMAIN = "rounds"
CHECKS_DOMAIN = "checks"
FRESHNESS = timedelta(minutes=15)
VALID_STATUSES = {"EN_ATTENTE", "PRESENT", "HORS_ZONE", "SANS_REPONSE"}


class Location(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude: float | None = None
    accuracy: float = Field(default=0, ge=0)
    address: str | None = None


class PresencePingIn(BaseModel):
    location: Location


def _now() -> datetime:
    return utcnow()


def _grace() -> timedelta:
    """Return the response grace period, configurable without a schema change."""
    raw = os.getenv("PRESENCE_CHECK_GRACE_SECONDS", os.getenv("PRESENCE_GRACE_SECONDS", "20"))
    try:
        seconds = max(0, int(raw))
    except (TypeError, ValueError):
        seconds = 20
    return timedelta(seconds=seconds)


def _as_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


def _parse_instant(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _day(value: Any) -> str:
    instant = _parse_instant(value)
    return instant.astimezone(TZ).date().isoformat() if instant else ""


def _date_filter(value: str | None, label: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(400, "VALIDATION_ERROR", f"La date {label} est invalide.")
        return None


def _haversine(latitude1: float, longitude1: float, latitude2: float, longitude2: float) -> float:
    radius = 6_371_000.0
    d_lat = radians(latitude2 - latitude1)
    d_lon = radians(longitude2 - longitude1)
    a = sin(d_lat / 2) ** 2 + cos(radians(latitude1)) * cos(radians(latitude2)) * sin(d_lon / 2) ** 2
    return 2 * radius * asin(sqrt(max(0.0, min(1.0, a))))


def _gps(workplace: Workplace | None, location: dict[str, Any]) -> dict[str, Any]:
    if workplace is None:
        return {"withinRadius": True, "distance": 0, "radiusMeters": 0}
    distance = round(_haversine(float(workplace.latitude), float(workplace.longitude), location["latitude"], location["longitude"]))
    return {
        "withinRadius": distance <= int(workplace.radius_meters),
        "distance": distance,
        "radiusMeters": int(workplace.radius_meters),
    }


def _tracking(db: Session, attendance_id: Any) -> dict[str, Any] | None:
    return store.row(db, TRACKING_DOMAIN, str(attendance_id))


def _last_location(db: Session, attendance: Attendance) -> tuple[dict[str, Any] | None, datetime | None]:
    tracking = _tracking(db, attendance.id)
    if not tracking:
        return None, None
    location = tracking.get("lastLocation")
    at = _parse_instant(tracking.get("lastLocationAt"))
    return (dict(location) if isinstance(location, dict) else None), at


def _emit(db: Session, event: dict[str, Any], recipients: set[str] | None = None) -> None:
    """Queue a realtime event when the host has the optional event queue installed."""
    try:
        # app.main in this repository predates the queue helper.  Initialising
        # it here preserves the store API and avoids making REST requests fail
        # when websocket delivery is not enabled.
        from . import main as main_module
        if not hasattr(main_module, "event_queue"):
            main_module.event_queue = []
        store.emit_after_commit(db, event, recipients)
    except Exception:
        # REST persistence must remain available when websocket infrastructure
        # is deliberately disabled (or in an isolated unit test).
        return


def _admin_ids(db: Session) -> set[str]:
    return {str(item.id) for item in db.scalars(select(User).where(User.role == Role.admin, User.active.is_(True)))}


def _employee_ids(db: Session, codes: list[str]) -> set[str]:
    if not codes:
        return set()
    return {
        str(item.id)
        for item in db.scalars(
            select(User).where(User.role == Role.employee, User.active.is_(True), User.employee_code.in_(codes))
        )
    }


def _round_out(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(value["id"]),
        "startedAt": value.get("startedAt"),
        "evaluatedAt": value.get("evaluatedAt"),
        "trigger": value.get("trigger"),
        "total": int(value.get("total", 0)),
        "present": int(value.get("present", 0)),
        "outOfZone": int(value.get("outOfZone", 0)),
        "noResponse": int(value.get("noResponse", 0)),
    }


def _check_out(db: Session, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(value["id"]),
        "roundId": str(value["roundId"]),
        "attendanceId": str(value["attendanceId"]) if value.get("attendanceId") else None,
        "employeeCode": value.get("employeeCode", ""),
        "employeeName": value.get("employeeName", ""),
        "departmentName": value.get("departmentName"),
        "siteId": str(value["siteId"]) if value.get("siteId") else None,
        "siteName": value.get("siteName"),
        "status": value.get("status", "EN_ATTENTE"),
        "location": value.get("location"),
        "locationAt": value.get("locationAt"),
        "distance": value.get("distance"),
        "radiusMeters": value.get("radiusMeters"),
        "checkedAt": value.get("checkedAt"),
    }


def _rounds(db: Session) -> list[dict[str, Any]]:
    return store.rows(db, ROUNDS_DOMAIN)


def _checks(db: Session) -> list[dict[str, Any]]:
    return store.rows(db, CHECKS_DOMAIN)


def _settings(db: Session) -> AppSettings | None:
    return tenant_singleton(db,AppSettings,1)


def start_round(db: Session, trigger: str, by: User | None = None) -> dict[str, Any]:
    """Create a round and one pending check for every currently open attendance."""
    if trigger not in {"AUTO", "MANUEL"}:
        raise ValueError("invalid presence round trigger")
    sites={str(site.get("id")):site for site in store.rows(db,"sites")}
    opened = list(
        db.scalars(
            select(Attendance).where(Attendance.status == AttendanceStatus.EN_COURS).order_by(Attendance.entry_time)
        )
    )
    started = iso(_now())
    round_value: dict[str, Any] = {
        "id": store.new_id(),
        "startedAt": started,
        "evaluatedAt": None,
        "trigger": trigger,
        "total": len(opened),
        "present": 0,
        "outOfZone": 0,
        "noResponse": 0,
    }
    store.save(db, ROUNDS_DOMAIN, round_value, key=round_value["id"])
    employee_codes: list[str] = []
    for attendance in opened:
        employee = db.get(Employee, attendance.employee_code)
        workplace = db.get(Workplace, attendance.workplace_id) if attendance.workplace_id else None
        check = {
            "id": store.new_id(),
            "roundId": round_value["id"],
            "attendanceId": str(attendance.id),
            "employeeCode": attendance.employee_code,
            "employeeName": f"{employee.first_name} {employee.last_name}" if employee else attendance.employee_code,
            "departmentName": attendance.department_name,
            "siteId": str(attendance.site_id) if attendance.site_id else None,
            "siteName": (sites.get(str(attendance.site_id)) or {}).get("name") if attendance.site_id else None,
            "status": "EN_ATTENTE",
            "location": None,
            "locationAt": None,
            "distance": None,
            "radiusMeters": int(workplace.radius_meters) if workplace else None,
            "checkedAt": None,
        }
        store.save(db, CHECKS_DOMAIN, check, key=check["id"])
        employee_codes.append(attendance.employee_code)

    summary = f"Contrôle de présence {'automatique' if trigger == 'AUTO' else 'lancé manuellement'} ({len(opened)} salarié{'s' if len(opened) != 1 else ''})"
    if by is not None:
        audit(db, by, "PRESENCE_CHECK_TRIGGERED", round_value["id"], summary)
    else:
        # Automatic rounds have no human actor, but remain auditable.
        from .models import AuditLog
        db.add(AuditLog(user_id=None, user_label="Système", action="PRESENCE_CHECK_TRIGGERED", target_id=round_value["id"], summary=summary))

    targets = _employee_ids(db, employee_codes)
    if targets:
        _emit(db, {"type": "presence.request", "roundId": round_value["id"]}, targets)
    admins = _admin_ids(db)
    if admins:
        _emit(db, {"type": "presence.updated"}, admins)
    return round_value


def evaluation_round(db: Session, round_value: dict[str, Any], now: datetime | None = None) -> bool:
    """Evaluate one elapsed round and maintain its exact status counters."""
    if round_value.get("evaluatedAt"):
        return False
    current = (now or _now()).astimezone(timezone.utc)
    started = _parse_instant(round_value.get("startedAt"))
    if started is None or current < started + _grace():
        return False

    checks = [item for item in _checks(db) if str(item.get("roundId")) == str(round_value["id"])]
    for check in checks:
        attendance = db.get(Attendance, _as_uuid(check.get("attendanceId"))) if check.get("attendanceId") else None
        location, location_at = _last_location(db, attendance) if attendance else (None, None)
        fresh = bool(location and location_at and started - FRESHNESS <= location_at <= current)
        check["checkedAt"] = iso(current)
        if not fresh:
            check["status"] = "SANS_REPONSE"
            check["location"] = None
            check["locationAt"] = None
            check["distance"] = None
            store.save(db, CHECKS_DOMAIN, check, key=str(check["id"]))
            continue

        check["location"] = {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "accuracy": location.get("accuracy", 0),
        }
        check["locationAt"] = iso(location_at)
        workplace = db.get(Workplace, attendance.workplace_id) if attendance and attendance.workplace_id else None
        if workplace is None:
            check["status"] = "PRESENT"
            check["distance"] = 0
            check["radiusMeters"] = None
        elif not workplace.geofencing_enabled:
            check["status"] = "PRESENT"
            check["distance"] = 0
            check["radiusMeters"] = int(workplace.radius_meters)
        else:
            result = _gps(workplace, location)
            check["distance"] = result["distance"]
            check["radiusMeters"] = result["radiusMeters"]
            check["status"] = "PRESENT" if result["withinRadius"] else "HORS_ZONE"
        store.save(db, CHECKS_DOMAIN, check, key=str(check["id"]))

    round_value["evaluatedAt"] = iso(current)
    round_value["present"] = sum(1 for item in checks if item.get("status") == "PRESENT")
    round_value["outOfZone"] = sum(1 for item in checks if item.get("status") == "HORS_ZONE")
    round_value["noResponse"] = sum(1 for item in checks if item.get("status") == "SANS_REPONSE")
    store.save(db, ROUNDS_DOMAIN, round_value, key=str(round_value["id"]))
    admins = _admin_ids(db)
    if admins:
        _emit(db, {"type": "presence.updated"}, admins)
    return True


def maybe_auto_round(db: Session, now: datetime | None = None) -> dict[str, Any] | None:
    """Start an automatic round when enabled and its configured interval elapsed."""
    config = _settings(db)
    if not config or not config.presence_check_enabled:
        return None
    opened = db.scalar(select(Attendance.id).where(Attendance.status == AttendanceStatus.EN_COURS).limit(1))
    if opened is None:
        return None
    current = (now or _now()).astimezone(timezone.utc)
    autos = [item for item in _rounds(db) if item.get("trigger") == "AUTO"]
    latest = max(((_parse_instant(item.get("startedAt")), item) for item in autos if _parse_instant(item.get("startedAt"))), default=(None, None))[1]
    interval = max(1, int(config.presence_check_interval_minutes or 1))
    if latest is not None:
        latest_at = _parse_instant(latest.get("startedAt"))
        if latest_at and current < latest_at + timedelta(minutes=interval):
            return None
    return start_round(db, "AUTO", None)


def presence_tick(db: Session, now: datetime | None = None) -> bool:
    """Reusable clock: evaluate elapsed rounds, then consider an automatic round."""
    # Une seule instance/lecture peut décider qu'une ronde automatique est due.
    store.lock_domain(db,"presence-scheduler","rounds")
    changed = False
    current = now or _now()
    for value in list(_rounds(db)):
        changed = evaluation_round(db, value, current) or changed
    auto = maybe_auto_round(db, current)
    if auto is not None:
        changed = True
    return changed


def _tick_and_commit(db: Session) -> None:
    if presence_tick(db):
        db.commit()


@router.post("/me/presence/ping")
def ping_presence(data: PresencePingIn, user: User = Depends(employee), db: Session = Depends(get_db)):
    attendance = db.scalar(
        select(Attendance)
        .where(Attendance.employee_code == user.employee_code, Attendance.status == AttendanceStatus.EN_COURS)
        .with_for_update()
    )
    if attendance is None:
        fail(409, "NO_OPEN_SESSION", "Aucune session de pointage ouverte.")

    location = data.location.model_dump(exclude_none=True)
    timestamp = _now()
    store.save(
        db,
        TRACKING_DOMAIN,
        {
            "attendanceId": str(attendance.id),
            "lastLocation": location,
            "lastLocationAt": iso(timestamp),
        },
        key=str(attendance.id),
    )
    workplace = db.get(Workplace, attendance.workplace_id) if attendance.workplace_id else None
    result = _gps(workplace, location)
    db.commit()
    return {
        "withinRadius": result["withinRadius"],
        "distance": result["distance"],
        "radiusMeters": result["radiusMeters"],
        "departmentName": attendance.department_name,
    }


@router.get("/presence/rounds")
def list_rounds(
    from_: str | None = Query(None, alias="from"),
    to_: str | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=1000),
    user: User = Depends(admin),
    db: Session = Depends(get_db),
):
    _tick_and_commit(db)
    start = _date_filter(from_, "from")
    end = _date_filter(to_, "to")
    values = []
    for value in _rounds(db):
        day = _parse_instant(value.get("startedAt"))
        local_day = day.astimezone(TZ).date() if day else None
        if start and (local_day is None or local_day < start):
            continue
        if end and (local_day is None or local_day > end):
            continue
        values.append(value)
    values.sort(key=lambda item: _parse_instant(item.get("startedAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [_round_out(value) for value in values[:limit]]


@router.get("/presence/checks")
def list_checks(
    roundId: str | None = None,
    date_: str | None = Query(None, alias="date"),
    employeeCode: str | None = None,
    siteId: str | None = None,
    status: str | None = None,
    user: User = Depends(admin),
    db: Session = Depends(get_db),
):
    _tick_and_commit(db)
    selected_date = _date_filter(date_, "date")
    if status and status not in VALID_STATUSES:
        fail(400, "VALIDATION_ERROR", "Le statut de contrôle est invalide.")
    round_days = {str(item.get("id")): _parse_instant(item.get("startedAt")) for item in _rounds(db)}
    values = []
    for value in _checks(db):
        if roundId and str(value.get("roundId")) != roundId:
            continue
        if employeeCode and value.get("employeeCode") != employeeCode:
            continue
        if siteId and str(value.get("siteId")) != siteId:
            continue
        if status and value.get("status") != status:
            continue
        started = round_days.get(str(value.get("roundId")))
        if selected_date and (started is None or started.astimezone(TZ).date() != selected_date):
            continue
        values.append(value)
    values.sort(key=lambda item: _parse_instant(item.get("checkedAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [_check_out(db, value) for value in values]


@router.post("/presence/rounds")
def trigger_round(user: User = Depends(admin), db: Session = Depends(get_db)):
    store.lock_domain(db,"presence-scheduler","rounds")
    _tick_and_commit(db)
    store.lock_domain(db,"presence-scheduler","rounds")
    if db.scalar(select(Attendance.id).where(Attendance.status == AttendanceStatus.EN_COURS).limit(1)) is None:
        fail(409, "NO_OPEN_SESSION", "Aucun salarié n'est en poste : rien à contrôler.")
    value = start_round(db, "MANUEL", user)
    db.commit()
    return _round_out(value)


# from __future__ import annotations

# """Presence controls.

# Rounds and checks deliberately live in :mod:`app.store` rather than in new ORM
# models.  This keeps this feature additive to the existing schema while still
# making every mutation transactional through the request's SQLAlchemy session.
# """

# import os
# from datetime import date, datetime, timedelta, timezone
# from math import asin, cos, radians, sin, sqrt
# from typing import Any
# from uuid import UUID
# from zoneinfo import ZoneInfo

# from fastapi import APIRouter, Depends, Query
# from pydantic import BaseModel, Field
# from sqlalchemy import select
# from sqlalchemy.orm import Session

# from . import store
# from .core import admin, audit, current_user, employee, fail, get_db, iso, settings
# from .models import AppSettings, Attendance, AttendanceStatus, Employee, Role, User, Workplace, utcnow


# router = APIRouter(tags=["presence"])
# TZ = ZoneInfo(settings.timezone_name)
# TRACKING_DOMAIN = "attendance_tracking"
# ROUNDS_DOMAIN = "rounds"
# CHECKS_DOMAIN = "checks"
# FRESHNESS = timedelta(minutes=15)
# VALID_STATUSES = {"EN_ATTENTE", "PRESENT", "HORS_ZONE", "SANS_REPONSE"}


# class Location(BaseModel):
#     latitude: float = Field(ge=-90, le=90)
#     longitude: float = Field(ge=-180, le=180)
#     altitude: float | None = None
#     accuracy: float = Field(default=0, ge=0)
#     address: str | None = None


# class PresencePingIn(BaseModel):
#     location: Location


# def _now() -> datetime:
#     return utcnow()


# def _grace() -> timedelta:
#     """Return the response grace period, configurable without a schema change."""
#     raw = os.getenv("PRESENCE_CHECK_GRACE_SECONDS", os.getenv("PRESENCE_GRACE_SECONDS", "20"))
#     try:
#         seconds = max(0, int(raw))
#     except (TypeError, ValueError):
#         seconds = 20
#     return timedelta(seconds=seconds)


# def _as_uuid(value: Any) -> UUID | None:
#     if value is None:
#         return None
#     try:
#         return value if isinstance(value, UUID) else UUID(str(value))
#     except (TypeError, ValueError):
#         return None


# def _parse_instant(value: Any) -> datetime | None:
#     if not value:
#         return None
#     try:
#         parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
#         if parsed.tzinfo is None:
#             parsed = parsed.replace(tzinfo=timezone.utc)
#         return parsed.astimezone(timezone.utc)
#     except (TypeError, ValueError):
#         return None


# def _day(value: Any) -> str:
#     instant = _parse_instant(value)
#     return instant.astimezone(TZ).date().isoformat() if instant else ""


# def _date_filter(value: str | None, label: str) -> date | None:
#     if value is None or value == "":
#         return None
#     try:
#         return date.fromisoformat(value)
#     except ValueError:
#         fail(400, "VALIDATION_ERROR", f"La date {label} est invalide.")
#         return None


# def _haversine(latitude1: float, longitude1: float, latitude2: float, longitude2: float) -> float:
#     radius = 6_371_000.0
#     d_lat = radians(latitude2 - latitude1)
#     d_lon = radians(longitude2 - longitude1)
#     a = sin(d_lat / 2) ** 2 + cos(radians(latitude1)) * cos(radians(latitude2)) * sin(d_lon / 2) ** 2
#     return 2 * radius * asin(sqrt(max(0.0, min(1.0, a))))


# def _gps(workplace: Workplace | None, location: dict[str, Any]) -> dict[str, Any]:
#     if workplace is None:
#         return {"withinRadius": True, "distance": 0, "radiusMeters": 0}
#     distance = round(_haversine(float(workplace.latitude), float(workplace.longitude), location["latitude"], location["longitude"]))
#     return {
#         "withinRadius": distance <= int(workplace.radius_meters),
#         "distance": distance,
#         "radiusMeters": int(workplace.radius_meters),
#     }


# def _tracking(db: Session, attendance_id: Any) -> dict[str, Any] | None:
#     return store.row(db, TRACKING_DOMAIN, str(attendance_id))


# def _last_location(db: Session, attendance: Attendance) -> tuple[dict[str, Any] | None, datetime | None]:
#     tracking = _tracking(db, attendance.id)
#     if not tracking:
#         return None, None
#     location = tracking.get("lastLocation")
#     at = _parse_instant(tracking.get("lastLocationAt"))
#     return (dict(location) if isinstance(location, dict) else None), at


# def _emit(db: Session, event: dict[str, Any], recipients: set[str] | None = None) -> None:
#     """Queue a realtime event when the host has the optional event queue installed."""
#     try:
#         # app.main in this repository predates the queue helper.  Initialising
#         # it here preserves the store API and avoids making REST requests fail
#         # when websocket delivery is not enabled.
#         from . import main as main_module
#         if not hasattr(main_module, "event_queue"):
#             main_module.event_queue = []
#         store.emit_after_commit(db, event, recipients)
#     except Exception:
#         # REST persistence must remain available when websocket infrastructure
#         # is deliberately disabled (or in an isolated unit test).
#         return


# def _admin_ids(db: Session) -> set[str]:
#     return {str(item.id) for item in db.scalars(select(User).where(User.role == Role.admin, User.active.is_(True)))}


# def _employee_ids(db: Session, codes: list[str]) -> set[str]:
#     if not codes:
#         return set()
#     return {
#         str(item.id)
#         for item in db.scalars(
#             select(User).where(User.role == Role.employee, User.active.is_(True), User.employee_code.in_(codes))
#         )
#     }


# def _round_out(value: dict[str, Any]) -> dict[str, Any]:
#     return {
#         "id": str(value["id"]),
#         "startedAt": value.get("startedAt"),
#         "evaluatedAt": value.get("evaluatedAt"),
#         "trigger": value.get("trigger"),
#         "total": int(value.get("total", 0)),
#         "present": int(value.get("present", 0)),
#         "outOfZone": int(value.get("outOfZone", 0)),
#         "noResponse": int(value.get("noResponse", 0)),
#     }


# def _check_out(db: Session, value: dict[str, Any]) -> dict[str, Any]:
#     return {
#         "id": str(value["id"]),
#         "roundId": str(value["roundId"]),
#         "attendanceId": str(value["attendanceId"]) if value.get("attendanceId") else None,
#         "employeeCode": value.get("employeeCode", ""),
#         "employeeName": value.get("employeeName", ""),
#         "departmentName": value.get("departmentName"),
#         "siteId": str(value["siteId"]) if value.get("siteId") else None,
#         "siteName": value.get("siteName"),
#         "status": value.get("status", "EN_ATTENTE"),
#         "location": value.get("location"),
#         "locationAt": value.get("locationAt"),
#         "distance": value.get("distance"),
#         "radiusMeters": value.get("radiusMeters"),
#         "checkedAt": value.get("checkedAt"),
#     }


# def _rounds(db: Session) -> list[dict[str, Any]]:
#     return store.rows(db, ROUNDS_DOMAIN)


# def _checks(db: Session) -> list[dict[str, Any]]:
#     return store.rows(db, CHECKS_DOMAIN)


# def _settings(db: Session) -> AppSettings | None:
#     return db.get(AppSettings, 1)


# def start_round(db: Session, trigger: str, by: User | None = None) -> dict[str, Any]:
#     """Create a round and one pending check for every currently open attendance."""
#     if trigger not in {"AUTO", "MANUEL"}:
#         raise ValueError("invalid presence round trigger")
#     sites={str(site.get("id")):site for site in store.rows(db,"sites")}
#     opened = list(
#         db.scalars(
#             select(Attendance).where(Attendance.status == AttendanceStatus.EN_COURS).order_by(Attendance.entry_time)
#         )
#     )
#     started = iso(_now())
#     round_value: dict[str, Any] = {
#         "id": store.new_id(),
#         "startedAt": started,
#         "evaluatedAt": None,
#         "trigger": trigger,
#         "total": len(opened),
#         "present": 0,
#         "outOfZone": 0,
#         "noResponse": 0,
#     }
#     store.save(db, ROUNDS_DOMAIN, round_value, key=round_value["id"])
#     employee_codes: list[str] = []
#     for attendance in opened:
#         employee = db.get(Employee, attendance.employee_code)
#         workplace = db.get(Workplace, attendance.workplace_id) if attendance.workplace_id else None
#         check = {
#             "id": store.new_id(),
#             "roundId": round_value["id"],
#             "attendanceId": str(attendance.id),
#             "employeeCode": attendance.employee_code,
#             "employeeName": f"{employee.first_name} {employee.last_name}" if employee else attendance.employee_code,
#             "departmentName": attendance.department_name,
#             "siteId": str(attendance.site_id) if attendance.site_id else None,
#             "siteName": (sites.get(str(attendance.site_id)) or {}).get("name") if attendance.site_id else None,
#             "status": "EN_ATTENTE",
#             "location": None,
#             "locationAt": None,
#             "distance": None,
#             "radiusMeters": int(workplace.radius_meters) if workplace else None,
#             "checkedAt": None,
#         }
#         store.save(db, CHECKS_DOMAIN, check, key=check["id"])
#         employee_codes.append(attendance.employee_code)

#     summary = f"Contrôle de présence {'automatique' if trigger == 'AUTO' else 'lancé manuellement'} ({len(opened)} salarié{'s' if len(opened) != 1 else ''})"
#     if by is not None:
#         audit(db, by, "PRESENCE_CHECK_TRIGGERED", round_value["id"], summary)
#     else:
#         # Automatic rounds have no human actor, but remain auditable.
#         from .models import AuditLog
#         db.add(AuditLog(user_id=None, user_label="Système", action="PRESENCE_CHECK_TRIGGERED", target_id=round_value["id"], summary=summary))

#     targets = _employee_ids(db, employee_codes)
#     if targets:
#         _emit(db, {"type": "presence.request", "roundId": round_value["id"]}, targets)
#     admins = _admin_ids(db)
#     if admins:
#         _emit(db, {"type": "presence.updated"}, admins)
#     return round_value


# def evaluation_round(db: Session, round_value: dict[str, Any], now: datetime | None = None) -> bool:
#     """Evaluate one elapsed round and maintain its exact status counters."""
#     if round_value.get("evaluatedAt"):
#         return False
#     current = (now or _now()).astimezone(timezone.utc)
#     started = _parse_instant(round_value.get("startedAt"))
#     if started is None or current < started + _grace():
#         return False

#     checks = [item for item in _checks(db) if str(item.get("roundId")) == str(round_value["id"])]
#     for check in checks:
#         attendance = db.get(Attendance, _as_uuid(check.get("attendanceId"))) if check.get("attendanceId") else None
#         location, location_at = _last_location(db, attendance) if attendance else (None, None)
#         fresh = bool(location and location_at and started - FRESHNESS <= location_at <= current)
#         check["checkedAt"] = iso(current)
#         if not fresh:
#             check["status"] = "SANS_REPONSE"
#             check["location"] = None
#             check["locationAt"] = None
#             check["distance"] = None
#             store.save(db, CHECKS_DOMAIN, check, key=str(check["id"]))
#             continue

#         check["location"] = {
#             "latitude": location["latitude"],
#             "longitude": location["longitude"],
#             "accuracy": location.get("accuracy", 0),
#         }
#         check["locationAt"] = iso(location_at)
#         workplace = db.get(Workplace, attendance.workplace_id) if attendance and attendance.workplace_id else None
#         if workplace is None:
#             check["status"] = "PRESENT"
#             check["distance"] = 0
#             check["radiusMeters"] = None
#         elif not workplace.geofencing_enabled:
#             check["status"] = "PRESENT"
#             check["distance"] = 0
#             check["radiusMeters"] = int(workplace.radius_meters)
#         else:
#             result = _gps(workplace, location)
#             check["distance"] = result["distance"]
#             check["radiusMeters"] = result["radiusMeters"]
#             check["status"] = "PRESENT" if result["withinRadius"] else "HORS_ZONE"
#         store.save(db, CHECKS_DOMAIN, check, key=str(check["id"]))

#     round_value["evaluatedAt"] = iso(current)
#     round_value["present"] = sum(1 for item in checks if item.get("status") == "PRESENT")
#     round_value["outOfZone"] = sum(1 for item in checks if item.get("status") == "HORS_ZONE")
#     round_value["noResponse"] = sum(1 for item in checks if item.get("status") == "SANS_REPONSE")
#     store.save(db, ROUNDS_DOMAIN, round_value, key=str(round_value["id"]))
#     admins = _admin_ids(db)
#     if admins:
#         _emit(db, {"type": "presence.updated"}, admins)
#     return True


# def maybe_auto_round(db: Session, now: datetime | None = None) -> dict[str, Any] | None:
#     """Start an automatic round when enabled and its configured interval elapsed."""
#     config = _settings(db)
#     if not config or not config.presence_check_enabled:
#         return None
#     opened = db.scalar(select(Attendance.id).where(Attendance.status == AttendanceStatus.EN_COURS).limit(1))
#     if opened is None:
#         return None
#     current = (now or _now()).astimezone(timezone.utc)
#     autos = [item for item in _rounds(db) if item.get("trigger") == "AUTO"]
#     latest = max(((_parse_instant(item.get("startedAt")), item) for item in autos if _parse_instant(item.get("startedAt"))), default=(None, None))[1]
#     interval = max(1, int(config.presence_check_interval_minutes or 1))
#     if latest is not None:
#         latest_at = _parse_instant(latest.get("startedAt"))
#         if latest_at and current < latest_at + timedelta(minutes=interval):
#             return None
#     return start_round(db, "AUTO", None)


# def presence_tick(db: Session, now: datetime | None = None) -> bool:
#     """Reusable clock: evaluate elapsed rounds, then consider an automatic round."""
#     # Une seule instance/lecture peut décider qu'une ronde automatique est due.
#     store.lock_domain(db,"presence-scheduler","rounds")
#     changed = False
#     current = now or _now()
#     for value in list(_rounds(db)):
#         changed = evaluation_round(db, value, current) or changed
#     auto = maybe_auto_round(db, current)
#     if auto is not None:
#         changed = True
#     return changed


# def _tick_and_commit(db: Session) -> None:
#     if presence_tick(db):
#         db.commit()


# @router.post("/me/presence/ping")
# def ping_presence(data: PresencePingIn, user: User = Depends(employee), db: Session = Depends(get_db)):
#     attendance = db.scalar(
#         select(Attendance)
#         .where(Attendance.employee_code == user.employee_code, Attendance.status == AttendanceStatus.EN_COURS)
#         .with_for_update()
#     )
#     if attendance is None:
#         fail(409, "NO_OPEN_SESSION", "Aucune session de pointage ouverte.")

#     location = data.location.model_dump(exclude_none=True)
#     timestamp = _now()
#     store.save(
#         db,
#         TRACKING_DOMAIN,
#         {
#             "attendanceId": str(attendance.id),
#             "lastLocation": location,
#             "lastLocationAt": iso(timestamp),
#         },
#         key=str(attendance.id),
#     )
#     workplace = db.get(Workplace, attendance.workplace_id) if attendance.workplace_id else None
#     result = _gps(workplace, location)
#     db.commit()
#     return {
#         "withinRadius": result["withinRadius"],
#         "distance": result["distance"],
#         "radiusMeters": result["radiusMeters"],
#         "departmentName": attendance.department_name,
#     }


# @router.get("/presence/rounds")
# def list_rounds(
#     from_: str | None = Query(None, alias="from"),
#     to_: str | None = Query(None, alias="to"),
#     limit: int = Query(100, ge=1, le=1000),
#     user: User = Depends(admin),
#     db: Session = Depends(get_db),
# ):
#     _tick_and_commit(db)
#     start = _date_filter(from_, "from")
#     end = _date_filter(to_, "to")
#     values = []
#     for value in _rounds(db):
#         day = _parse_instant(value.get("startedAt"))
#         local_day = day.astimezone(TZ).date() if day else None
#         if start and (local_day is None or local_day < start):
#             continue
#         if end and (local_day is None or local_day > end):
#             continue
#         values.append(value)
#     values.sort(key=lambda item: _parse_instant(item.get("startedAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
#     return [_round_out(value) for value in values[:limit]]


# @router.get("/presence/checks")
# def list_checks(
#     roundId: str | None = None,
#     date_: str | None = Query(None, alias="date"),
#     employeeCode: str | None = None,
#     siteId: str | None = None,
#     status: str | None = None,
#     user: User = Depends(admin),
#     db: Session = Depends(get_db),
# ):
#     _tick_and_commit(db)
#     selected_date = _date_filter(date_, "date")
#     if status and status not in VALID_STATUSES:
#         fail(400, "VALIDATION_ERROR", "Le statut de contrôle est invalide.")
#     round_days = {str(item.get("id")): _parse_instant(item.get("startedAt")) for item in _rounds(db)}
#     values = []
#     for value in _checks(db):
#         if roundId and str(value.get("roundId")) != roundId:
#             continue
#         if employeeCode and value.get("employeeCode") != employeeCode:
#             continue
#         if siteId and str(value.get("siteId")) != siteId:
#             continue
#         if status and value.get("status") != status:
#             continue
#         started = round_days.get(str(value.get("roundId")))
#         if selected_date and (started is None or started.astimezone(TZ).date() != selected_date):
#             continue
#         values.append(value)
#     values.sort(key=lambda item: _parse_instant(item.get("checkedAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
#     return [_check_out(db, value) for value in values]


# @router.post("/presence/rounds")
# def trigger_round(user: User = Depends(admin), db: Session = Depends(get_db)):
#     store.lock_domain(db,"presence-scheduler","rounds")
#     _tick_and_commit(db)
#     store.lock_domain(db,"presence-scheduler","rounds")
#     if db.scalar(select(Attendance.id).where(Attendance.status == AttendanceStatus.EN_COURS).limit(1)) is None:
#         fail(409, "NO_OPEN_SESSION", "Aucun salarié n'est en poste : rien à contrôler.")
#     value = start_round(db, "MANUEL", user)
#     db.commit()
#     return _round_out(value)
