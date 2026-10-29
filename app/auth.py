from datetime import datetime, timezone, timedelta
import hashlib, hmac
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, func, delete, text
from sqlalchemy.orm import Session
from .core import get_db, settings, fail, current_user, admin, hash_password, verify_password, issue_access, issue_refresh, user_payload, utcnow, set_tenant_context
from .models import User, Employee, RefreshToken, Role, AuthLoginAttempt
from .core import audit
router=APIRouter(prefix="/auth",tags=["auth"])
class LoginIn(BaseModel): identifier:str=Field(min_length=1,max_length=255); password:str=Field(min_length=1,max_length=1024)
class AdminIn(BaseModel): email:EmailStr; password:str=Field(min_length=6,max_length=1024); inviteCode:str=Field(min_length=1,max_length=256)
class PasswordIn(BaseModel): password:str=Field(min_length=1,max_length=1024)

def _attempt_key(ip:str,identity:str)->str:
    raw=f"{ip}|{identity.strip().lower()}".encode()
    return hmac.new(settings.jwt_access_secret.encode(),raw,hashlib.sha256).hexdigest()

def _check_rate(db:Session,key:str):
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),{"key":"fortico:login:"+key})
    cutoff=utcnow()-timedelta(minutes=15)
    db.execute(delete(AuthLoginAttempt).where(AuthLoginAttempt.attempted_at<cutoff))
    count=int(db.scalar(select(func.count()).select_from(AuthLoginAttempt).where(AuthLoginAttempt.key_hash==key,AuthLoginAttempt.attempted_at>=cutoff)) or 0)
    if count>=5: fail(429,"RATE_LIMITED","Trop de tentatives. Réessayez dans 15 minutes.")

def _failed(db:Session,key:str):
    db.add(AuthLoginAttempt(key_hash=key,attempted_at=utcnow()));db.commit()

def set_refresh_cookie(response:Response,token:str):
    response.set_cookie("refresh_token",token,httponly=True,secure=settings.cookie_secure,samesite=settings.cookie_samesite,path="/api/v1/auth",max_age=settings.refresh_ttl_days*86400)
@router.post('/login')
def login(data:LoginIn,request:Request,response:Response,db:Session=Depends(get_db)):
    identifier=data.identifier.strip().lower()
    ip=request.client.host if request.client else "unknown"
    key=_attempt_key(ip,identifier)
    set_tenant_context(db, None, auth_lookup=True)
    _check_rate(db,key)
    if '@' in identifier:
        user=db.scalar(select(User).where(func.lower(User.email)==identifier))
    else:
        user=db.scalar(select(User).where(func.lower(User.employee_code)==identifier))
    if not user or not verify_password(data.password,user.password_hash): _failed(db,key);fail(401,"INVALID_CREDENTIALS","Identifiant ou mot de passe incorrect.")
    set_tenant_context(db, user.tenant_id)
    if user.role==Role.employee and (not user.active or not db.get(Employee,user.employee_code) or not db.get(Employee,user.employee_code).active): fail(403,"ACCOUNT_DISABLED","Votre compte est désactivé. Contactez votre administrateur.")
    if not user.active: _failed(db,key);fail(401,"INVALID_CREDENTIALS","Identifiant ou mot de passe incorrect.")
    db.execute(delete(AuthLoginAttempt).where(AuthLoginAttempt.key_hash==key))
    token=issue_refresh(db,user); db.commit(); set_refresh_cookie(response,token)
    return {"accessToken":issue_access(user),"user":user_payload(user,db)}
@router.post('/register-admin')
def register_admin(data:AdminIn,request:Request,response:Response,db:Session=Depends(get_db)):
    key=_attempt_key(request.client.host if request.client else "unknown",data.email)
    set_tenant_context(db, None, auth_lookup=True)
    _check_rate(db,key)
    if not secrets_equal(data.inviteCode,settings.invite_code): _failed(db,key);fail(403,"INVALID_INVITE_CODE","Code d'invitation administrateur incorrect.")
    email=data.email.lower()
    if db.scalar(select(User).where(func.lower(User.email)==email)): fail(409,"EMAIL_TAKEN","Cet email est déjà utilisé.")
    admin_id=uuid4()
    set_tenant_context(db, admin_id)
    user=User(id=admin_id,tenant_id=admin_id,role=Role.admin,email=email,password_hash=hash_password(data.password)); db.add(user); db.flush()
    audit(db,user,"ADMIN_CREATED",str(user.id),f"Compte administrateur {email} créé")
    token=issue_refresh(db,user); db.commit(); set_refresh_cookie(response,token)
    return {"accessToken":issue_access(user),"user":user_payload(user,db)}
def secrets_equal(a,b):
    import hmac
    return hmac.compare_digest(a,b)
@router.post('/refresh')
def refresh(request:Request,response:Response,db:Session=Depends(get_db)):
    raw=request.cookies.get('refresh_token')
    if not raw: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
    set_tenant_context(db, None, auth_lookup=True)
    digest=hashlib.sha256(raw.encode()).hexdigest()
    row=db.scalar(select(RefreshToken).where(RefreshToken.token_hash==digest,RefreshToken.revoked_at.is_(None),RefreshToken.expires_at>utcnow()).with_for_update())
    if not row: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
    user=db.get(User,row.user_id)
    if not user or not user.active: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
    set_tenant_context(db, user.tenant_id)
    row.revoked_at=utcnow(); new=issue_refresh(db,user); db.commit(); set_refresh_cookie(response,new)
    return {"accessToken":issue_access(user)}
@router.get('/me')
def me(user:User=Depends(current_user),db:Session=Depends(get_db)): return user_payload(user,db)
@router.post('/logout',status_code=204)
def logout(request:Request,response:Response,db:Session=Depends(get_db)):
    raw=request.cookies.get('refresh_token')
    if raw:
        set_tenant_context(db, None, auth_lookup=True)
        row=db.scalar(select(RefreshToken).where(RefreshToken.token_hash==hashlib.sha256(raw.encode()).hexdigest()))
        if row:
            set_tenant_context(db, row.tenant_id)
            row.revoked_at=utcnow(); db.commit()
    response.delete_cookie('refresh_token',path='/api/v1/auth',httponly=True,secure=settings.cookie_secure,samesite=settings.cookie_samesite)
@router.post('/verify-password',status_code=204)
def verify_admin(data:PasswordIn,user:User=Depends(admin)):
    if not verify_password(data.password,user.password_hash): fail(403,"INVALID_PASSWORD","Mot de passe incorrect.")


# from datetime import datetime, timezone, timedelta
# import hashlib, hmac
# from uuid import UUID
# from fastapi import APIRouter, Depends, Request, Response
# from pydantic import BaseModel, EmailStr, Field
# from sqlalchemy import select, func, delete, text
# from sqlalchemy.orm import Session
# from .core import get_db, settings, fail, current_user, admin, hash_password, verify_password, issue_access, issue_refresh, user_payload, utcnow
# from .models import User, Employee, RefreshToken, Role, AuthLoginAttempt
# from .core import audit
# router=APIRouter(prefix="/auth",tags=["auth"])
# class LoginIn(BaseModel): identifier:str=Field(min_length=1,max_length=255); password:str=Field(min_length=1,max_length=1024)
# class AdminIn(BaseModel): email:EmailStr; password:str=Field(min_length=6,max_length=1024); inviteCode:str=Field(min_length=1,max_length=256)
# class PasswordIn(BaseModel): password:str=Field(min_length=1,max_length=1024)

# def _attempt_key(ip:str,identity:str)->str:
#     raw=f"{ip}|{identity.strip().lower()}".encode()
#     return hmac.new(settings.jwt_access_secret.encode(),raw,hashlib.sha256).hexdigest()

# def _check_rate(db:Session,key:str):
#     db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),{"key":"fortico:login:"+key})
#     cutoff=utcnow()-timedelta(minutes=15)
#     db.execute(delete(AuthLoginAttempt).where(AuthLoginAttempt.attempted_at<cutoff))
#     count=int(db.scalar(select(func.count()).select_from(AuthLoginAttempt).where(AuthLoginAttempt.key_hash==key,AuthLoginAttempt.attempted_at>=cutoff)) or 0)
#     if count>=5: fail(429,"RATE_LIMITED","Trop de tentatives. Réessayez dans 15 minutes.")

# def _failed(db:Session,key:str):
#     db.add(AuthLoginAttempt(key_hash=key,attempted_at=utcnow()));db.commit()

# def set_refresh_cookie(response:Response,token:str):
#     response.set_cookie("refresh_token",token,httponly=True,secure=settings.cookie_secure,samesite=settings.cookie_samesite,path="/api/v1/auth",max_age=settings.refresh_ttl_days*86400)
# @router.post('/login')
# def login(data:LoginIn,request:Request,response:Response,db:Session=Depends(get_db)):
#     identifier=data.identifier.strip().lower()
#     ip=request.client.host if request.client else "unknown"
#     key=_attempt_key(ip,identifier);_check_rate(db,key)
#     if '@' in identifier:
#         user=db.scalar(select(User).where(func.lower(User.email)==identifier))
#     else:
#         user=db.scalar(select(User).where(func.lower(User.employee_code)==identifier))
#     if not user or not verify_password(data.password,user.password_hash): _failed(db,key);fail(401,"INVALID_CREDENTIALS","Identifiant ou mot de passe incorrect.")
#     if user.role==Role.employee and (not user.active or not db.get(Employee,user.employee_code) or not db.get(Employee,user.employee_code).active): fail(403,"ACCOUNT_DISABLED","Votre compte est désactivé. Contactez votre administrateur.")
#     if not user.active: _failed(db,key);fail(401,"INVALID_CREDENTIALS","Identifiant ou mot de passe incorrect.")
#     db.execute(delete(AuthLoginAttempt).where(AuthLoginAttempt.key_hash==key))
#     token=issue_refresh(db,user); db.commit(); set_refresh_cookie(response,token)
#     return {"accessToken":issue_access(user),"user":user_payload(user,db)}
# @router.post('/register-admin')
# def register_admin(data:AdminIn,request:Request,response:Response,db:Session=Depends(get_db)):
#     key=_attempt_key(request.client.host if request.client else "unknown",data.email);_check_rate(db,key)
#     if not secrets_equal(data.inviteCode,settings.invite_code): _failed(db,key);fail(403,"INVALID_INVITE_CODE","Code d'invitation administrateur incorrect.")
#     email=data.email.lower()
#     if db.scalar(select(User).where(func.lower(User.email)==email)): fail(409,"EMAIL_TAKEN","Cet email est déjà utilisé.")
#     user=User(role=Role.admin,email=email,password_hash=hash_password(data.password)); db.add(user); db.flush()
#     audit(db,user,"ADMIN_CREATED",str(user.id),f"Compte administrateur {email} créé")
#     token=issue_refresh(db,user); db.commit(); set_refresh_cookie(response,token)
#     return {"accessToken":issue_access(user),"user":user_payload(user,db)}
# def secrets_equal(a,b):
#     import hmac
#     return hmac.compare_digest(a,b)
# @router.post('/refresh')
# def refresh(request:Request,response:Response,db:Session=Depends(get_db)):
#     raw=request.cookies.get('refresh_token')
#     if not raw: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
#     digest=hashlib.sha256(raw.encode()).hexdigest()
#     row=db.scalar(select(RefreshToken).where(RefreshToken.token_hash==digest,RefreshToken.revoked_at.is_(None),RefreshToken.expires_at>utcnow()).with_for_update())
#     if not row: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
#     user=db.get(User,row.user_id)
#     if not user or not user.active: fail(401,"UNAUTHENTICATED","Session expirée. Veuillez vous reconnecter.")
#     row.revoked_at=utcnow(); new=issue_refresh(db,user); db.commit(); set_refresh_cookie(response,new)
#     return {"accessToken":issue_access(user)}
# @router.get('/me')
# def me(user:User=Depends(current_user),db:Session=Depends(get_db)): return user_payload(user,db)
# @router.post('/logout',status_code=204)
# def logout(request:Request,response:Response,db:Session=Depends(get_db)):
#     raw=request.cookies.get('refresh_token')
#     if raw:
#         row=db.scalar(select(RefreshToken).where(RefreshToken.token_hash==hashlib.sha256(raw.encode()).hexdigest()))
#         if row: row.revoked_at=utcnow(); db.commit()
#     response.delete_cookie('refresh_token',path='/api/v1/auth',httponly=True,secure=settings.cookie_secure,samesite=settings.cookie_samesite)
# @router.post('/verify-password',status_code=204)
# def verify_admin(data:PasswordIn,user:User=Depends(admin)):
#     if not verify_password(data.password,user.password_hash): fail(403,"INVALID_PASSWORD","Mot de passe incorrect.")
