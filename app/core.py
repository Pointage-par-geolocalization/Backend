from datetime import datetime, timedelta, timezone
import hashlib, secrets
import jwt
from pwdlib import PasswordHash
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from pydantic_settings import BaseSettings, SettingsConfigDict
from .models import User, Employee, RefreshToken, utcnow

class Settings(BaseSettings):
    model_config=SettingsConfigDict(env_file=".env", extra="ignore")
    database_url:str="postgresql+psycopg://pointage:pointage@localhost:5432/pointage"
    jwt_access_secret:str="CHANGE_ME_TO_A_LONG_RANDOM_SECRET"
    jwt_access_ttl_minutes:int=15
    refresh_ttl_days:int=30
    invite_code:str="Pointage_202"
    frontend_origins:str="http://localhost:5173"
    cookie_secure:bool=False
    cookie_samesite:str="lax"
    timezone_name:str="Indian/Antananarivo"
    environment:str="development"
settings=Settings()
engine=create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal=sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
password_hash=PasswordHash.recommended()

def get_db():
    db=SessionLocal()
    try: yield db
    finally: db.close()

def fail(status:int, code:str, message:str, details=None):
    raise HTTPException(status_code=status, detail={"code":code,"message":message,"details":details})

def api_error_handler(request, exc):
    from fastapi.responses import JSONResponse
    detail=exc.detail if isinstance(exc.detail,dict) else {"code":"ERROR","message":str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error":detail})

def hash_password(p:str)->str: return password_hash.hash(p)
def verify_password(p:str,h:str)->bool:
    try:return password_hash.verify(p,h)
    except Exception:return False

def issue_access(user:User)->str:
    return jwt.encode({"sub":str(user.id),"role":user.role.value,"employeeCode":user.employee_code,"ver":user.token_version,"exp":datetime.now(timezone.utc)+timedelta(minutes=settings.jwt_access_ttl_minutes)},settings.jwt_access_secret,algorithm="HS256")
def issue_refresh(db:Session,user:User)->str:
    raw=secrets.token_urlsafe(48)
    db.add(RefreshToken(user_id=user.id,token_hash=hashlib.sha256(raw.encode()).hexdigest(),expires_at=utcnow()+timedelta(days=settings.refresh_ttl_days)))
    return raw

def current_user(request:Request, db:Session=Depends(get_db))->User:
    auth=request.headers.get("authorization","")
    if not auth.startswith("Bearer "): fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
    try:
        payload=jwt.decode(auth[7:],settings.jwt_access_secret,algorithms=["HS256"])
        from uuid import UUID
        user=db.get(User,UUID(payload["sub"]))
    except Exception: user=None; payload={}
    if not user or payload.get("ver")!=user.token_version: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
    if user.role.value=="employee" and not user.active: fail(403,"ACCOUNT_DISABLED","Votre compte est désactivé. Contactez votre administrateur.")
    if user.role.value=="admin" and not user.active: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
    if user.role.value=="employee":
        emp=db.get(Employee,user.employee_code)
        if not emp or not emp.active: fail(403,"ACCOUNT_DISABLED","Ce compte salarié est désactivé.")
    return user

def admin(user:User=Depends(current_user))->User:
    if user.role.value!="admin": fail(403,"FORBIDDEN","Accès réservé à l'administrateur.")
    return user

def employee(user:User=Depends(current_user))->User:
    if user.role.value!="employee": fail(403,"FORBIDDEN","Accès réservé aux salariés.")
    return user

def user_payload(user:User,db:Session)->dict:
    emp=db.get(Employee,user.employee_code) if user.employee_code else None
    return {"id":str(user.id),"role":user.role.value,"employeeCode":user.employee_code,"email":user.email if user.role.value=="admin" else None,"name":f"{emp.first_name} {emp.last_name}" if emp else ("Administrateur" if user.role.value=="admin" else None)}

def iso(dt): return dt.isoformat(timespec="milliseconds").replace("+00:00","Z") if dt else None
def employee_payload(e:Employee,db:Session)->dict:
    return {"employeeCode":e.employee_code,"firstName":e.first_name,"lastName":e.last_name,"email":e.email,"phone":e.phone,"position":e.position or "","hireDate":e.hire_date.isoformat() if e.hire_date else None,"address":e.address,"baseDailySalary":e.base_daily_salary,"contractType":e.contract_type,"monthlySalary":e.monthly_salary,"active":e.active,"departmentIds":[str(w.id) for w in e.workplaces],"createdAt":iso(e.created_at)}

def audit(db,user,action,target_id=None,summary=None):
    from .models import AuditLog
    label=user.email if user.role.value=="admin" else ((lambda e:f"{e.first_name} {e.last_name}")(db.get(Employee,user.employee_code)) if user.employee_code and db.get(Employee,user.employee_code) else "Salarié")
    db.add(AuditLog(user_id=user.id,user_label=label,action=action,target_id=str(target_id) if target_id else None,summary=summary))



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
#     invite_code:str="CHANGE_ME"
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
#     return {"employeeCode":e.employee_code,"firstName":e.first_name,"lastName":e.last_name,"email":e.email,"phone":e.phone,"position":e.position or "","hireDate":e.hire_date.isoformat() if e.hire_date else None,"address":e.address,"baseDailySalary":e.base_daily_salary,"active":e.active,"departmentIds":[str(w.id) for w in e.workplaces],"createdAt":iso(e.created_at)}

# def audit(db,user,action,target_id=None,summary=None):
#     from .models import AuditLog
#     label=user.email if user.role.value=="admin" else ((lambda e:f"{e.first_name} {e.last_name}")(db.get(Employee,user.employee_code)) if user.employee_code and db.get(Employee,user.employee_code) else "Salarié")
#     db.add(AuditLog(user_id=user.id,user_label=label,action=action,target_id=str(target_id) if target_id else None,summary=summary))
