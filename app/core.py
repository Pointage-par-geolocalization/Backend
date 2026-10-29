from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from uuid import UUID

import jwt
from pwdlib import PasswordHash
from fastapi import Depends, HTTPException, Request
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker, with_loader_criteria
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import User, Employee, RefreshToken, TenantScoped, utcnow


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://pointage:pointage@localhost:5432/pointage"
    jwt_access_secret: str = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"
    jwt_access_ttl_minutes: int = 15
    refresh_ttl_days: int = 30
    invite_code: str = "Pointage_202"
    frontend_origins: str = "http://localhost:5173"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    timezone_name: str = "Indian/Antananarivo"
    environment: str = "development"


settings = Settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
password_hash = PasswordHash.recommended()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def set_tenant_context(db: Session, tenant_id, *, auth_lookup: bool = False) -> None:
    """Set the immutable tenant scope for this request and the current PG transaction."""
    if tenant_id is not None and not isinstance(tenant_id, UUID):
        tenant_id = UUID(str(tenant_id))
    db.info["tenant_id"] = tenant_id
    db.info["auth_lookup"] = bool(auth_lookup)
    if db.in_transaction() and db.get_bind().dialect.name == "postgresql":
        db.connection().execute(
            text("SELECT set_config('app.tenant_id', :tenant, true), "
                 "set_config('app.auth_lookup', :auth_lookup, true)"),
            {"tenant": str(tenant_id) if tenant_id else "", "auth_lookup": "on" if auth_lookup else "off"},
        )


@event.listens_for(Session, "after_begin")
def _install_transaction_tenant_context(session, transaction, connection):
    if connection.dialect.name != "postgresql":
        return
    tenant_id = session.info.get("tenant_id")
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant, true), "
             "set_config('app.auth_lookup', :auth_lookup, true)"),
        {"tenant": str(tenant_id) if tenant_id else "",
         "auth_lookup": "on" if session.info.get("auth_lookup") else "off"},
    )


@event.listens_for(Session, "do_orm_execute")
def _apply_orm_tenant_filter(execute_state):
    """Add a tenant predicate to ORM reads; PostgreSQL RLS also protects bulk/raw SQL."""
    if not execute_state.is_select:
        return
    session = execute_state.session
    tenant_id = session.info.get("tenant_id")
    if tenant_id is None and session.info.get("auth_lookup"):
        return
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda model: model.tenant_id == tenant_id,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _enforce_tenant_on_writes(session, flush_context, instances):
    tenant_id = session.info.get("tenant_id")
    for obj in session.new:
        if isinstance(obj, TenantScoped):
            if tenant_id is None:
                raise RuntimeError("Tenant context required to create tenant-owned records")
            # Ignore any tenant value supplied by a request payload.
            obj.tenant_id = tenant_id
    for obj in session.dirty:
        if isinstance(obj, TenantScoped):
            if tenant_id is None or obj.tenant_id != tenant_id:
                raise RuntimeError("Cross-tenant record modification blocked")
            from sqlalchemy import inspect
            if inspect(obj).attrs.tenant_id.history.has_changes():
                raise RuntimeError("Tenant ownership is immutable")


def tenant_singleton(db: Session, model, key=1):
    tenant_id = db.info.get("tenant_id")
    if tenant_id is None:
        return None
    return db.scalar(select(model).where(model.tenant_id == tenant_id, model.id == key))


def fail(status: int, code: str, message: str, details=None):
    raise HTTPException(status_code=status, detail={"code": code, "message": message, "details": details})


def api_error_handler(request, exc):
    from fastapi.responses import JSONResponse
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "ERROR", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return password_hash.verify(password, stored_hash)
    except Exception:
        return False


def issue_access(user: User) -> str:
    return jwt.encode(
        {"sub": str(user.id), "role": user.role.value, "employeeCode": user.employee_code,
         "tenantId": str(user.tenant_id), "ver": user.token_version,
         "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_ttl_minutes)},
        settings.jwt_access_secret, algorithm="HS256",
    )


def issue_refresh(db: Session, user: User) -> str:
    raw = secrets.token_urlsafe(48)
    db.add(RefreshToken(user_id=user.id, tenant_id=user.tenant_id,
                        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                        expires_at=utcnow() + timedelta(days=settings.refresh_ttl_days)))
    return raw


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        fail(401, "UNAUTHENTICATED", "Session expirée. Veuillez vous reconnecter.")
    try:
        payload = jwt.decode(auth[7:], settings.jwt_access_secret, algorithms=["HS256"])
        user_id = UUID(payload["sub"])
        claimed_tenant = UUID(payload["tenantId"]) if payload.get("tenantId") else None
        # Legacy access tokens have no tenant claim; allow one authenticated identity lookup,
        # then immediately bind the database transaction to the tenant stored on that user.
        set_tenant_context(db, claimed_tenant, auth_lookup=claimed_tenant is None)
        user = db.get(User, user_id)
    except Exception:
        user = None
        payload = {}
        claimed_tenant = None
    if not user or payload.get("ver") != user.token_version:
        fail(401, "UNAUTHENTICATED", "Session expirée. Veuillez vous reconnecter.")
    if claimed_tenant is None:
        set_tenant_context(db, user.tenant_id)
    elif user.tenant_id != claimed_tenant:
        fail(401, "UNAUTHENTICATED", "Session invalide. Veuillez vous reconnecter.")
    if user.role.value == "employee" and not user.active:
        fail(403, "ACCOUNT_DISABLED", "Votre compte est désactivé. Contactez votre administrateur.")
    if user.role.value == "admin" and not user.active:
        fail(401, "UNAUTHENTICATED", "Session expirée. Veuillez vous reconnecter.")
    if user.role.value == "employee":
        emp = db.get(Employee, user.employee_code)
        if not emp or not emp.active:
            fail(403, "ACCOUNT_DISABLED", "Ce compte salarié est désactivé.")
    return user


def admin(user: User = Depends(current_user)) -> User:
    if user.role.value != "admin":
        fail(403, "FORBIDDEN", "Accès réservé à l'administrateur.")
    return user


def employee(user: User = Depends(current_user)) -> User:
    if user.role.value != "employee":
        fail(403, "FORBIDDEN", "Accès réservé aux salariés.")
    return user


def user_payload(user: User, db: Session) -> dict:
    emp = db.get(Employee, user.employee_code) if user.employee_code else None
    return {"id": str(user.id), "role": user.role.value, "employeeCode": user.employee_code,
            "email": user.email if user.role.value == "admin" else None,
            "name": f"{emp.first_name} {emp.last_name}" if emp else ("Administrateur" if user.role.value == "admin" else None)}


def iso(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z") if dt else None


def employee_payload(employee_record: Employee, db: Session) -> dict:
    return {"employeeNumber": employee_record.employee_number,
            "employeeCode": employee_record.employee_code, "firstName": employee_record.first_name,
            "lastName": employee_record.last_name, "email": employee_record.email,
            "phone": employee_record.phone, "position": employee_record.position or "",
            "hireDate": employee_record.hire_date.isoformat() if employee_record.hire_date else None,
            "address": employee_record.address, "baseDailySalary": employee_record.base_daily_salary,
            "contractType": employee_record.contract_type, "monthlySalary": employee_record.monthly_salary,
            "active": employee_record.active,
            "departmentIds": [str(workplace.id) for workplace in employee_record.workplaces],
            "createdAt": iso(employee_record.created_at)}


def audit(db, user, action, target_id=None, summary=None):
    from .models import AuditLog
    label = user.email if user.role.value == "admin" else "Salarié"
    if user.employee_code:
        emp = db.get(Employee, user.employee_code)
        if emp:
            label = f"{emp.first_name} {emp.last_name}"
    db.add(AuditLog(user_id=user.id, user_label=label, action=action,
                    target_id=str(target_id) if target_id else None, summary=summary))


# from datetime import datetime, timedelta, timezone
# import hashlib, secrets
# import jwt
# from pwdlib import PasswordHash
# from fastapi import Depends, HTTPException, Request, Response
# from sqlalchemy import create_engine, select
# from sqlalchemy.orm import Session, sessionmaker
# from pydantic_settings import BaseSettings, SettingsConfigDict
# from .models import User, Employee, RefreshToken, utcnow

# class Settings(BaseSettings):
#     model_config=SettingsConfigDict(env_file=".env", extra="ignore")
#     database_url:str="postgresql+psycopg://pointage:pointage@localhost:5432/pointage"
#     jwt_access_secret:str="CHANGE_ME_TO_A_LONG_RANDOM_SECRET"
#     jwt_access_ttl_minutes:int=15
#     refresh_ttl_days:int=30
#     invite_code:str="Pointage_202"
#     frontend_origins:str="http://localhost:5173"
#     cookie_secure:bool=False
#     cookie_samesite:str="lax"
#     timezone_name:str="Indian/Antananarivo"
#     environment:str="development"
# settings=Settings()
# engine=create_engine(settings.database_url, pool_pre_ping=True)
# SessionLocal=sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
# password_hash=PasswordHash.recommended()

# def get_db():
#     db=SessionLocal()
#     try: yield db
#     finally: db.close()

# def fail(status:int, code:str, message:str, details=None):
#     raise HTTPException(status_code=status, detail={"code":code,"message":message,"details":details})

# def api_error_handler(request, exc):
#     from fastapi.responses import JSONResponse
#     detail=exc.detail if isinstance(exc.detail,dict) else {"code":"ERROR","message":str(exc.detail)}
#     return JSONResponse(status_code=exc.status_code, content={"error":detail})

# def hash_password(p:str)->str: return password_hash.hash(p)
# def verify_password(p:str,h:str)->bool:
#     try:return password_hash.verify(p,h)
#     except Exception:return False

# def issue_access(user:User)->str:
#     return jwt.encode({"sub":str(user.id),"role":user.role.value,"employeeCode":user.employee_code,"ver":user.token_version,"exp":datetime.now(timezone.utc)+timedelta(minutes=settings.jwt_access_ttl_minutes)},settings.jwt_access_secret,algorithm="HS256")
# def issue_refresh(db:Session,user:User)->str:
#     raw=secrets.token_urlsafe(48)
#     db.add(RefreshToken(user_id=user.id,token_hash=hashlib.sha256(raw.encode()).hexdigest(),expires_at=utcnow()+timedelta(days=settings.refresh_ttl_days)))
#     return raw

# def current_user(request:Request, db:Session=Depends(get_db))->User:
#     auth=request.headers.get("authorization","")
#     if not auth.startswith("Bearer "): fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
#     try:
#         payload=jwt.decode(auth[7:],settings.jwt_access_secret,algorithms=["HS256"])
#         from uuid import UUID
#         user=db.get(User,UUID(payload["sub"]))
#     except Exception: user=None; payload={}
#     if not user or payload.get("ver")!=user.token_version: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
#     if user.role.value=="employee" and not user.active: fail(403,"ACCOUNT_DISABLED","Votre compte est désactivé. Contactez votre administrateur.")
#     if user.role.value=="admin" and not user.active: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
#     if user.role.value=="employee":
#         emp=db.get(Employee,user.employee_code)
#         if not emp or not emp.active: fail(403,"ACCOUNT_DISABLED","Ce compte salarié est désactivé.")
#     return user

# def admin(user:User=Depends(current_user))->User:
#     if user.role.value!="admin": fail(403,"FORBIDDEN","Accès réservé à l'administrateur.")
#     return user

# def employee(user:User=Depends(current_user))->User:
#     if user.role.value!="employee": fail(403,"FORBIDDEN","Accès réservé aux salariés.")
#     return user

# def user_payload(user:User,db:Session)->dict:
#     emp=db.get(Employee,user.employee_code) if user.employee_code else None
#     return {"id":str(user.id),"role":user.role.value,"employeeCode":user.employee_code,"email":user.email if user.role.value=="admin" else None,"name":f"{emp.first_name} {emp.last_name}" if emp else ("Administrateur" if user.role.value=="admin" else None)}

# def iso(dt): return dt.isoformat(timespec="milliseconds").replace("+00:00","Z") if dt else None
# def employee_payload(e:Employee,db:Session)->dict:
#     return {"employeeNumber":e.employee_number,"employeeCode":e.employee_code,"firstName":e.first_name,"lastName":e.last_name,"email":e.email,"phone":e.phone,"position":e.position or "","hireDate":e.hire_date.isoformat() if e.hire_date else None,"address":e.address,"baseDailySalary":e.base_daily_salary,"contractType":e.contract_type,"monthlySalary":e.monthly_salary,"active":e.active,"departmentIds":[str(w.id) for w in e.workplaces],"createdAt":iso(e.created_at)}

# def audit(db,user,action,target_id=None,summary=None):
#     from .models import AuditLog
#     label=user.email if user.role.value=="admin" else ((lambda e:f"{e.first_name} {e.last_name}")(db.get(Employee,user.employee_code)) if user.employee_code and db.get(Employee,user.employee_code) else "Salarié")
#     db.add(AuditLog(user_id=user.id,user_label=label,action=action,target_id=str(target_id) if target_id else None,summary=summary))
