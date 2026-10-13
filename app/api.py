from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from math import radians,sin,cos,asin,sqrt
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session, selectinload
from .core import get_db, current_user, admin, employee, fail, employee_payload, iso, audit, hash_password, settings
from .models import User, Employee, Workplace, EmployeeWorkplace, Attendance, AttendanceStatus, PunchSource, SalaryRecord, Payment, AppSettings, Role, utcnow
router=APIRouter(tags=["application"])
TZ=ZoneInfo(settings.timezone_name)
class EmployeeCreate(BaseModel): employeeCode:str; firstName:str; lastName:str; email:str|None=None; phone:str|None=None; position:str|None=""; hireDate:date|None=None; address:str|None=None; baseDailySalary:int=Field(ge=0); departmentIds:list[str]=Field(min_length=1); password:str=Field(min_length=6)
class EmployeePatch(BaseModel): firstName:str|None=None; lastName:str|None=None; email:str|None=None; phone:str|None=None; position:str|None=None; hireDate:date|None=None; address:str|None=None; baseDailySalary:int|None=Field(default=None,ge=0); departmentIds:list[str]|None=None
class ActiveIn(BaseModel): active:bool
class PasswordUpdate(BaseModel): newPassword:str=Field(min_length=6)
class WorkplaceIn(BaseModel): name:str; latitude:float=Field(ge=-90,le=90); longitude:float=Field(ge=-180,le=180); radiusMeters:int=Field(default=100,ge=1,le=100000); geofencingEnabled:bool=True
class WorkplacePatch(BaseModel): name:str|None=None; latitude:float|None=Field(default=None,ge=-90,le=90); longitude:float|None=Field(default=None,ge=-180,le=180); radiusMeters:int|None=Field(default=None,ge=1,le=100000); geofencingEnabled:bool|None=None
class Location(BaseModel): latitude:float=Field(ge=-90,le=90); longitude:float=Field(ge=-180,le=180); altitude:float|None=None; accuracy:float=Field(default=0,ge=0); address:str|None=None
class ClockIn(BaseModel): workplaceId:UUID; location:Location
class ClockOut(BaseModel): location:Location
class AdminClockIn(BaseModel): employeeCode:str=Field(min_length=1,max_length=30); workplaceId:UUID
class AdminClockOut(BaseModel): exitTime:datetime|None=None
class PaymentIn(BaseModel): employeeCode:str; amount:int=Field(gt=0); note:str|None=None
class SettingsIn(BaseModel): dailySalary:int|None=Field(default=None,ge=0); normalWorkMinutes:int|None=Field(default=None,gt=0,le=1440); toleranceMinutes:int|None=Field(default=None,ge=0); normalEntryTime:str|None=None; presenceCheckEnabled:bool|None=None; presenceCheckIntervalMinutes:int|None=Field(default=None,ge=15,le=720); companyName:str|None=None; companyAddress:str|None=None; companyPhone:str|None=None; companyEmail:str|None=None; companyNif:str|None=None; companyStat:str|None=None; defaultTaxRate:float|None=Field(default=None,ge=0,le=100); documentFooter:str|None=None

def workplace_out(w): return {"id":str(w.id),"name":w.name,"latitude":float(w.latitude),"longitude":float(w.longitude),"radiusMeters":w.radius_meters,"geofencingEnabled":w.geofencing_enabled}
def haversine(lat1,lon1,lat2,lon2):
    r=6371000; a=radians(lat2-lat1); b=radians(lon2-lon1); h=sin(a/2)**2+cos(radians(lat1))*cos(radians(lat2))*sin(b/2)**2
    return 2*r*asin(sqrt(h))
def day_bounds(now=None):
    here=(now or utcnow()).astimezone(TZ); start=datetime.combine(here.date(),time.min,TZ).astimezone(timezone.utc); return here.date(),start,start+timedelta(days=1)
def gps(w,loc):
    distance=round(haversine(float(w.latitude),float(w.longitude),loc.latitude,loc.longitude),1); within=distance<=w.radius_meters
    return {"employeeLatitude":loc.latitude,"employeeLongitude":loc.longitude,"departmentLatitude":float(w.latitude),"departmentLongitude":float(w.longitude),"withinRadius":within,"distance":distance,"radiusMeters":w.radius_meters}
def att_out(a,db,with_names=False):
    emp=db.get(Employee,a.employee_code); w=db.get(Workplace,a.workplace_id) if a.workplace_id else None
    from .store import rows
    site=next((x for x in rows(db,"sites") if x.get("id")==str(a.site_id)),None) if a.site_id else None
    result={"id":str(a.id),"employeeCode":a.employee_code,"workDate":a.entry_time.astimezone(TZ).date().isoformat(),"workplaceId":str(a.workplace_id) if a.workplace_id else None,"departmentName":a.department_name,"siteId":str(a.site_id) if a.site_id else None,"status":a.status.value,"entryTime":iso(a.entry_time),"exitTime":iso(a.exit_time),"workedMinutes":a.worked_minutes,"dailySalary":a.daily_salary,"entrySource":a.entry_source.value,"exitSource":a.exit_source.value if a.exit_source else None,"entryLocation":a.entry_location,"exitLocation":a.exit_location,"entryGpsCheck":a.entry_gps_check,"exitGpsCheck":a.exit_gps_check}
    if with_names: result.update(employeeName=f"{emp.first_name} {emp.last_name}" if emp else "",siteName=site.get("name") if site else None)
    return result
def balance(db,code):
    earned=db.scalar(select(func.coalesce(func.sum(SalaryRecord.amount),0)).where(SalaryRecord.employee_code==code)) or 0
    paid=db.scalar(select(func.coalesce(func.sum(Payment.amount),0)).where(Payment.employee_code==code)) or 0
    return {"earned":int(earned),"paid":int(paid),"due":int(earned-paid)}
def ensure_employee_access(user,code):
    if user.role==Role.employee and user.employee_code!=code: fail(403,"FORBIDDEN","Accès réservé à vos données.")

def parse_uuid(value,label="identifiant"):
    try:return UUID(str(value))
    except (ValueError,TypeError,AttributeError):fail(400,"VALIDATION_ERROR",f"Le {label} est invalide.")

def check_workplace(db,code,wid):
    try: parsed=UUID(str(wid))
    except (ValueError,TypeError,AttributeError): fail(400,"VALIDATION_ERROR","Identifiant du lieu invalide.")
    w=db.get(Workplace,parsed)
    if not w: fail(404,"NOT_FOUND","Lieu de travail introuvable.")
    if not db.get(Employee,code) or not any(str(x.id)==str(w.id) for x in db.get(Employee,code).workplaces): fail(422,"DEPARTMENT_NOT_ALLOWED","Vous n'êtes pas autorisé à pointer sur ce lieu.")
    return w

def calculate_daily_salary(db,code,work_date,minutes):
    e=db.get(Employee,code); st=work_date
    prev=db.scalars(select(SalaryRecord.minutes).where(SalaryRecord.employee_code==code,SalaryRecord.date==st)).all(); all_minutes=sum(prev)+minutes
    cfg=db.get(AppSettings,1); normal=cfg.normal_work_minutes if cfg else 480
    from math import floor
    total=floor(e.base_daily_salary*min(all_minutes,normal)/normal+0.5) if normal else 0
    previously=sum(db.scalars(select(SalaryRecord.amount).where(SalaryRecord.employee_code==code,SalaryRecord.date==st)).all())
    return max(0,total-previously)

@router.get('/employees')
def employees(active:bool|None=None,q:str|None=None,departmentId:str|None=None,user:User=Depends(admin),db:Session=Depends(get_db)):
    stmt=select(Employee).options(selectinload(Employee.workplaces)).order_by(Employee.last_name,Employee.first_name)
    if active is not None: stmt=stmt.where(Employee.active==active)
    if q: stmt=stmt.where((Employee.first_name.ilike(f"%{q}%"))|(Employee.last_name.ilike(f"%{q}%"))|(Employee.employee_code.ilike(f"%{q}%")))
    if departmentId: stmt=stmt.join(Employee.workplaces).where(Workplace.id==parse_uuid(departmentId,"lieu de travail"))
    return [employee_payload(e,db) for e in db.scalars(stmt).unique()]
@router.get('/employees/{code}')
def get_employee(code:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    ensure_employee_access(user,code); e=db.scalar(select(Employee).options(selectinload(Employee.workplaces)).where(Employee.employee_code==code))
    if not e: fail(404,"NOT_FOUND","Salarié introuvable.")
    return employee_payload(e,db)
@router.get('/me/employee')
def my_employee(user:User=Depends(employee),db:Session=Depends(get_db)): return get_employee(user.employee_code,user,db)
@router.post('/employees',status_code=201)
def create_employee(data:EmployeeCreate,user:User=Depends(admin),db:Session=Depends(get_db)):
    code=data.employeeCode.strip()
    if not code or len(code)>30: fail(400,"VALIDATION_ERROR","L'identifiant salarié est obligatoire et limité à 30 caractères.")
    if not data.firstName.strip() or not data.lastName.strip(): fail(400,"VALIDATION_ERROR","Le prénom et le nom sont obligatoires.")
    if db.scalar(select(Employee.employee_code).where(func.lower(Employee.employee_code)==code.lower())): fail(409,"EMPLOYEE_CODE_TAKEN","Cet identifiant est déjà utilisé par un autre salarié.")
    ws=[]
    for wid in data.departmentIds:
        w=db.get(Workplace,parse_uuid(wid,"lieu de travail"))
        if not w: fail(400,"VALIDATION_ERROR","Un lieu de travail est invalide.")
        ws.append(w)
    email=data.email.strip().lower() if data.email and data.email.strip() else None
    if email and db.scalar(select(User.id).where(func.lower(User.email)==email)): fail(409,"EMAIL_TAKEN","Cette adresse e-mail est déjà utilisée.")
    e=Employee(employee_code=code,first_name=data.firstName.strip(),last_name=data.lastName.strip(),email=email,phone=data.phone,position=data.position or "",hire_date=data.hireDate,address=data.address,base_daily_salary=data.baseDailySalary,workplaces=ws)
    db.add(e); db.flush(); db.add(User(role=Role.employee,employee_code=code,email=email,password_hash=hash_password(data.password))); audit(db,user,"EMPLOYEE_CREATED",code,f"Salarié {e.first_name} {e.last_name} ({code}) créé"); db.commit(); return employee_payload(e,db)
@router.patch('/employees/{code}')
def patch_employee(code:str,data:EmployeePatch,user:User=Depends(admin),db:Session=Depends(get_db)):
    from .store import lock_domain
    lock_domain(db,"employees",code)
    e=db.get(Employee,code)
    if not e: fail(404,"NOT_FOUND","Salarié introuvable.")
    account=db.scalar(select(User).where(User.employee_code==code))
    fields=data.model_fields_set
    for field,key in [("firstName","first_name"),("lastName","last_name"),("email","email"),("phone","phone"),("position","position"),("hireDate","hire_date"),("address","address"),("baseDailySalary","base_daily_salary")]:
        if field in fields:
            v=getattr(data,field)
            if field in ("firstName","lastName") and (v is None or not v.strip()): fail(400,"VALIDATION_ERROR","Le prénom et le nom ne peuvent pas être vides.")
            if field=="baseDailySalary" and v is None: fail(400,"VALIDATION_ERROR","Le salaire journalier ne peut pas être nul.")
            if field=="email":
                email=v.strip().lower() if v and v.strip() else None
                if email and db.scalar(select(User.id).where(func.lower(User.email)==email,User.id!=account.id if account else True)): fail(409,"EMAIL_TAKEN","Cette adresse e-mail est déjà utilisée.")
                e.email=email
                if account: account.email=email
            else: setattr(e,key,v.strip() if field in ("firstName","lastName") else (v or "" if field=="position" else v))
    if "departmentIds" in fields:
        if data.departmentIds is None: fail(400,"VALIDATION_ERROR","Au moins un lieu de travail est requis.")
        if not data.departmentIds: fail(400,"VALIDATION_ERROR","Au moins un lieu de travail est requis.")
        selected=[db.get(Workplace,parse_uuid(i,"lieu de travail")) for i in data.departmentIds]
        if any(w is None for w in selected): fail(400,"VALIDATION_ERROR","Un lieu de travail est invalide.")
        e.workplaces=selected
    audit(db,user,"EMPLOYEE_UPDATED",code,"Fiche salarié modifiée");db.commit();return employee_payload(e,db)
@router.patch('/employees/{code}/active',status_code=204)
def active_employee(code:str,data:ActiveIn,user:User=Depends(admin),db:Session=Depends(get_db)):
    from .store import lock_domain
    lock_domain(db,"employees",code)
    e=db.get(Employee,code); u=db.scalar(select(User).where(User.employee_code==code))
    if not e or not u:fail(404,"NOT_FOUND","Salarié introuvable.")
    e.active=data.active;u.active=data.active;u.token_version+=1
    if not data.active: db.execute(__import__('sqlalchemy').update(__import__('app.models',fromlist=['RefreshToken']).RefreshToken).where(__import__('app.models',fromlist=['RefreshToken']).RefreshToken.user_id==u.id).values(revoked_at=utcnow()))
    audit(db,user,"EMPLOYEE_ENABLED" if data.active else "EMPLOYEE_DISABLED",code,"Statut du salarié modifié");uid=str(u.id);db.commit()
    if not data.active:
        from .store import emit_after_commit
        emit_after_commit(db,{"type":"session.revoked"},{uid})
@router.put('/employees/{code}/password',status_code=204)
def employee_password(code:str,data:PasswordUpdate,user:User=Depends(admin),db:Session=Depends(get_db)):
    from .store import lock_domain
    lock_domain(db,"employees",code)
    u=db.scalar(select(User).where(User.employee_code==code))
    if not u:fail(404,"NOT_FOUND","Salarié introuvable.")
    u.password_hash=hash_password(data.newPassword);u.token_version+=1
    from .models import RefreshToken
    db.execute(__import__('sqlalchemy').update(RefreshToken).where(RefreshToken.user_id==u.id).values(revoked_at=utcnow()))
    audit(db,user,"EMPLOYEE_PASSWORD_CHANGED",code,"Mot de passe de salarié modifié");uid=str(u.id);db.commit()
    from .store import emit_after_commit
    emit_after_commit(db,{"type":"session.revoked"},{uid})
@router.delete('/employees/{code}',status_code=204)
def delete_employee(code:str,user:User=Depends(admin),db:Session=Depends(get_db)):
    from .store import lock_domain
    lock_domain(db,"employees",code)
    e=db.get(Employee,code)
    if not e:fail(404,"NOT_FOUND","Salarié introuvable.")
    if db.scalar(select(Attendance.id).where(Attendance.employee_code==code).limit(1)) or db.scalar(select(Payment.id).where(Payment.employee_code==code).limit(1)):fail(409,"HAS_HISTORY","Ce salarié possède un historique. Désactivez-le plutôt.")
    u=db.scalar(select(User).where(User.employee_code==code))
    if u:
        from .store import rows,save,remove
        uid=str(u.id)
        for chat in rows(db,"chats"):
            if chat.get("directUserId")==uid:
                chat_id=str(chat.get("id"))
                for m in rows(db,"messages"):
                    if m.get("chatId")==chat_id: remove(db,"messages",str(m.get("id")))
                for r in rows(db,"reads"):
                    if r.get("chatId")==chat_id: remove(db,"reads",f"{chat_id}:{r.get('userId')}")
                remove(db,"chats",chat_id)
            elif uid in chat.get("memberIds",[]):
                chat["memberIds"]=[x for x in chat.get("memberIds",[]) if x!=uid];save(db,"chats",chat,str(chat["id"]))
        for domain in ("site_members","leaves","attendance_tracking"):
            for item in rows(db,domain):
                if item.get("employeeCode")==code or (domain=="attendance_tracking" and item.get("employeeCode")==code):remove(db,domain,str(item.get("id") or item.get("attendanceId")))
    else: uid=None
    audit(db,user,"EMPLOYEE_DELETED",code,f"Salarié {e.first_name} {e.last_name} ({code}) supprimé");db.delete(e);db.commit()
    if uid:
        from .store import emit_after_commit
        emit_after_commit(db,{"type":"session.revoked"},{uid})
@router.get('/employees/{code}/balance')
def employee_balance(code:str,user:User=Depends(current_user),db:Session=Depends(get_db)):ensure_employee_access(user,code);return balance(db,code)
@router.get('/me/balance')
def my_balance(user:User=Depends(employee),db:Session=Depends(get_db)):return balance(db,user.employee_code)

@router.get('/workplaces')
def workplaces(user:User=Depends(current_user),db:Session=Depends(get_db)):return [workplace_out(w) for w in db.scalars(select(Workplace).order_by(Workplace.name))]
@router.post('/workplaces',status_code=201)
def create_workplace(data:WorkplaceIn,user:User=Depends(admin),db:Session=Depends(get_db)):
    w=Workplace(name=data.name,latitude=data.latitude,longitude=data.longitude,radius_meters=data.radiusMeters,geofencing_enabled=data.geofencingEnabled);db.add(w);db.flush();audit(db,user,"WORKPLACE_CREATED",w.id,w.name);db.commit();return workplace_out(w)
@router.patch('/workplaces/{wid}')
def update_workplace(wid:str,data:WorkplacePatch,user:User=Depends(admin),db:Session=Depends(get_db)):
    from .store import lock_domain
    lock_domain(db,"workplaces",wid)
    w=db.get(Workplace,parse_uuid(wid,"lieu de travail"));
    if not w:fail(404,"NOT_FOUND","Lieu de travail introuvable.")
    for src,dst in [("name","name"),("latitude","latitude"),("longitude","longitude"),("radiusMeters","radius_meters"),("geofencingEnabled","geofencing_enabled")]:
        if src in data.model_fields_set:
            value=getattr(data,src)
            if value is None: fail(400,"VALIDATION_ERROR",f"Le champ {src} ne peut pas être nul.")
            if src=="name" and not value.strip(): fail(400,"VALIDATION_ERROR","Le nom du lieu est obligatoire.")
            setattr(w,dst,value.strip() if src=="name" else value)
    audit(db,user,"WORKPLACE_UPDATED",w.id,w.name);db.commit();return workplace_out(w)
@router.delete('/workplaces/{wid}',status_code=204)
def delete_workplace(wid:str,user:User=Depends(admin),db:Session=Depends(get_db)):
    from .store import lock_domain,rows,save
    lock_domain(db,"workplaces",wid)
    w=db.get(Workplace,parse_uuid(wid,"lieu de travail"));
    if not w:fail(404,"NOT_FOUND","Lieu de travail introuvable.")
    for site in rows(db,"sites"):
        if site.get("workplaceId")==str(w.id): site["workplaceId"]=None;save(db,"sites",site,str(site["id"]))
    audit(db,user,"WORKPLACE_DELETED",w.id,w.name);db.delete(w);db.commit()
@router.get('/settings')
def get_settings(user:User=Depends(current_user),db:Session=Depends(get_db)):
    s=db.get(AppSettings,1)
    if not s:return {"dailySalary":20000,"normalWorkMinutes":480,"toleranceMinutes":10,"normalEntryTime":"08:00","presenceCheckEnabled":True,"presenceCheckIntervalMinutes":60,"companyName":"","companyAddress":"","companyPhone":"","companyEmail":"","companyNif":"","companyStat":"","defaultTaxRate":0,"documentFooter":""}
    return settings_out(s)
def settings_out(s):return {"dailySalary":s.daily_salary,"normalWorkMinutes":s.normal_work_minutes,"toleranceMinutes":s.tolerance_minutes,"normalEntryTime":s.normal_entry_time,"presenceCheckEnabled":s.presence_check_enabled,"presenceCheckIntervalMinutes":s.presence_check_interval_minutes,"companyName":s.company_name or "","companyAddress":s.company_address or "","companyPhone":s.company_phone or "","companyEmail":s.company_email or "","companyNif":s.company_nif or "","companyStat":s.company_stat or "","defaultTaxRate":float(s.default_tax_rate or 0),"documentFooter":s.document_footer or ""}
@router.put('/settings')
def put_settings(data:SettingsIn,user:User=Depends(admin),db:Session=Depends(get_db)):
    s=db.get(AppSettings,1) or AppSettings(id=1);db.add(s)
    for a,b in [("dailySalary","daily_salary"),("normalWorkMinutes","normal_work_minutes"),("toleranceMinutes","tolerance_minutes"),("normalEntryTime","normal_entry_time"),("presenceCheckEnabled","presence_check_enabled"),("presenceCheckIntervalMinutes","presence_check_interval_minutes"),("companyName","company_name"),("companyAddress","company_address"),("companyPhone","company_phone"),("companyEmail","company_email"),("companyNif","company_nif"),("companyStat","company_stat"),("defaultTaxRate","default_tax_rate"),("documentFooter","document_footer")]:
        if a in data.model_fields_set:
            value=getattr(data,a)
            if value is None and not (a.startswith("company") or a=="documentFooter"):
                fail(400,"VALIDATION_ERROR",f"Le champ {a} ne peut pas être nul.")
            if a=="normalEntryTime" and value is not None:
                try: datetime.strptime(value,"%H:%M")
                except ValueError: fail(400,"VALIDATION_ERROR","Heure d'entrée invalide (HH:mm).")
            if a.startswith("company") or a=="documentFooter": value=value or ""
            setattr(s,b,value)
    audit(db,user,"SETTINGS_UPDATED",1,"Paramètres modifiés");db.commit();return settings_out(s)

@router.post('/attendance/clock-in')
def clock_in(data:ClockIn,user:User=Depends(employee),db:Session=Depends(get_db)):
    code=user.employee_code
    from .store import lock_domain
    lock_domain(db,"attendance",code)
    if db.scalar(select(Attendance.id).where(Attendance.employee_code==code,Attendance.status==AttendanceStatus.EN_COURS)):fail(409,"ALREADY_CLOCKED_IN","Vous avez déjà un pointage en cours.")
    w=check_workplace(db,code,data.workplaceId);check=gps(w,data.location)
    if w.geofencing_enabled and not check['withinRadius']:fail(422,"GPS_OUT_OF_RANGE","Pointage refusé — vous êtes en dehors du rayon autorisé.",{"distance":check['distance'],"radiusMeters":w.radius_meters})
    at=Attendance(employee_code=code,workplace_id=w.id,department_name=w.name,status=AttendanceStatus.EN_COURS,entry_time=utcnow(),entry_source=PunchSource.EMPLOYEE,entry_location=data.location.model_dump(),entry_gps_check=check)
    from .sites_api import _effective_site_id
    site_id=_effective_site_id(db,w.id,at.entry_time,None)
    if site_id:
        try: at.site_id=UUID(site_id)
        except (ValueError,TypeError): pass
    db.add(at);db.commit();db.refresh(at)
    from .store import emit_after_commit
    emit_after_commit(db,{"type":"attendance.updated"})
    return {"attendance":att_out(at,db),"gpsCheck":check}
@router.post('/attendance/clock-out')
def clock_out(data:ClockOut,user:User=Depends(employee),db:Session=Depends(get_db)):
    from .store import lock_domain
    lock_domain(db,"attendance",user.employee_code)
    at=db.scalar(select(Attendance).where(Attendance.employee_code==user.employee_code,Attendance.status==AttendanceStatus.EN_COURS).with_for_update())
    if not at:fail(409,"NO_OPEN_SESSION","Aucun pointage en cours.")
    w=db.get(Workplace,at.workplace_id) if at.workplace_id else None; check=gps(w,data.location) if w else {"withinRadius":True,"distance":0,"radiusMeters":0,"location":data.location.model_dump()}
    if w and w.geofencing_enabled and not check['withinRadius']:fail(422,"GPS_OUT_OF_RANGE","Pointage refusé — vous êtes en dehors du rayon autorisé.",{"distance":check['distance'],"radiusMeters":w.radius_meters})
    now=utcnow();mins=max(0,round((now-at.entry_time).total_seconds()/60));workday=at.entry_time.astimezone(TZ).date();amount=calculate_daily_salary(db,user.employee_code,workday,mins)
    at.exit_time=now;at.status=AttendanceStatus.TERMINEE;at.worked_minutes=mins;at.daily_salary=amount;at.exit_source=PunchSource.EMPLOYEE;at.exit_location=data.location.model_dump();at.exit_gps_check=check
    prev=int(db.scalar(select(func.coalesce(func.sum(SalaryRecord.amount),0)).where(SalaryRecord.employee_code==user.employee_code)) or 0);db.add(SalaryRecord(attendance_id=at.id,employee_code=user.employee_code,date=workday,minutes=mins,amount=amount,cumulative_salary=prev+amount))
    from .store import emit_after_commit, remove
    remove(db,"attendance_tracking",str(at.id))
    db.commit()
    emit_after_commit(db,{"type":"attendance.updated"})
    return {"workedMinutes":mins,"dailySalary":amount,"gpsCheck":check}
@router.get('/me/attendance/today')
def today_att(user:User=Depends(employee),db:Session=Depends(get_db)):
    d,start,end=day_bounds(); rows=db.scalars(select(Attendance).where(Attendance.employee_code==user.employee_code,Attendance.entry_time>=start,Attendance.entry_time<end).order_by(Attendance.entry_time)).all();return [att_out(a,db) for a in rows]
@router.get('/me/attendance')
def my_att(limit:int=Query(60,ge=1,le=300),user:User=Depends(employee),db:Session=Depends(get_db)):
    rows=db.scalars(select(Attendance).where(Attendance.employee_code==user.employee_code).order_by(Attendance.entry_time.desc()).limit(limit)).all();return [att_out(a,db) for a in rows]
@router.get('/attendance')
def list_att(date_:str|None=Query(None,alias='date'),from_:str|None=Query(None,alias='from'),to_:str|None=Query(None,alias='to'),employeeCode:str|None=None,siteId:str|None=None,user:User=Depends(admin),db:Session=Depends(get_db)):
    stmt=select(Attendance).order_by(Attendance.entry_time.desc())
    def valid_day(v,label):
        try:return date.fromisoformat(v)
        except (ValueError,TypeError):fail(400,"VALIDATION_ERROR",f"La date {label} est invalide.")
    if date_:
        target=valid_day(date_,"date"); ds=datetime.combine(target,time.min,TZ).astimezone(timezone.utc); de=ds+timedelta(days=1);stmt=stmt.where(Attendance.entry_time>=ds,Attendance.entry_time<de)
    if from_:
        target=valid_day(from_,"from"); ds=datetime.combine(target,time.min,TZ).astimezone(timezone.utc);stmt=stmt.where(Attendance.entry_time>=ds)
    if to_:
        target=valid_day(to_,"to"); de=datetime.combine(target+timedelta(days=1),time.min,TZ).astimezone(timezone.utc);stmt=stmt.where(Attendance.entry_time<de)
    if employeeCode:stmt=stmt.where(Attendance.employee_code==employeeCode)
    if siteId:
        try: site_uuid=UUID(siteId)
        except (ValueError,TypeError):fail(400,"VALIDATION_ERROR","Identifiant chantier invalide.")
        stmt=stmt.where(Attendance.site_id==site_uuid)
    return [att_out(a,db,True) for a in db.scalars(stmt.order_by(None).order_by(Attendance.entry_time.asc()))]
@router.post('/admin/attendance/clock-in')
def admin_clock_in(data:AdminClockIn,user:User=Depends(admin),db:Session=Depends(get_db)):
    code=data.employeeCode.strip(); emp=db.get(Employee,code)
    if not emp:fail(404,"NOT_FOUND","Salarié introuvable.")
    if not emp.active:fail(422,"ACCOUNT_DISABLED","Ce salarié est désactivé.")
    from .store import lock_domain
    lock_domain(db,"attendance",code)
    w=db.get(Workplace,data.workplaceId)
    if not w:fail(404,"NOT_FOUND","Lieu de travail introuvable.")
    if db.scalar(select(Attendance.id).where(Attendance.employee_code==code,Attendance.status==AttendanceStatus.EN_COURS)):fail(409,"ALREADY_CLOCKED_IN","Ce salarié a déjà un pointage en cours.")
    a=Attendance(employee_code=code,workplace_id=w.id,department_name=w.name,entry_time=utcnow(),entry_source=PunchSource.ADMIN)
    from .store import emit_after_commit
    from .sites_api import _effective_site_id
    site_id=_effective_site_id(db,w.id,a.entry_time,None)
    if site_id:
        try:a.site_id=UUID(site_id)
        except (ValueError,TypeError):pass
    db.add(a);db.flush();audit(db,user,"ATTENDANCE_CREATED",a.id,f"Pointage démarré à distance pour {emp.first_name} {emp.last_name} ({w.name})");attendance_id=str(a.id);db.commit();emit_after_commit(db,{"type":"attendance.updated"});return {"id":attendance_id}
@router.post('/admin/attendance/{aid}/clock-out')
def admin_clock_out(aid:UUID,data:AdminClockOut,user:User=Depends(admin),db:Session=Depends(get_db)):
    a=db.get(Attendance,aid);
    if not a:fail(404,"NOT_FOUND","Pointage introuvable.")
    from .store import lock_domain
    lock_domain(db,"attendance",a.employee_code)
    a=db.scalar(select(Attendance).where(Attendance.id==aid).with_for_update())
    if a.status!=AttendanceStatus.EN_COURS:fail(409,"ALREADY_FINISHED","Ce pointage est déjà terminé.")
    exit_time=data.exitTime or utcnow()
    if exit_time.tzinfo is None: exit_time=exit_time.replace(tzinfo=TZ)
    if exit_time<a.entry_time or exit_time>utcnow():fail(400,"VALIDATION_ERROR","Heure de sortie invalide.")
    mins=max(0,round((exit_time-a.entry_time).total_seconds()/60));d=a.entry_time.astimezone(TZ).date();amount=calculate_daily_salary(db,a.employee_code,d,mins);a.exit_time=exit_time;a.status=AttendanceStatus.TERMINEE;a.worked_minutes=mins;a.daily_salary=amount;a.exit_source=PunchSource.ADMIN
    prev=int(db.scalar(select(func.coalesce(func.sum(SalaryRecord.amount),0)).where(SalaryRecord.employee_code==a.employee_code)) or 0);db.add(SalaryRecord(attendance_id=a.id,employee_code=a.employee_code,date=d,minutes=mins,amount=amount,cumulative_salary=prev+amount));audit(db,user,"ATTENDANCE_UPDATED",a.id,"Pointage arrêté à distance")
    from .store import emit_after_commit, remove
    remove(db,"attendance_tracking",str(a.id))
    db.commit()
    emit_after_commit(db,{"type":"attendance.updated"})
    return {"workedMinutes":mins,"dailySalary":amount}
@router.get('/me/salary')
def my_salary(user:User=Depends(employee),db:Session=Depends(get_db)):
    rows=db.scalars(select(SalaryRecord).where(SalaryRecord.employee_code==user.employee_code).order_by(SalaryRecord.created_at.desc())).all();bal=balance(db,user.employee_code);return {"records":[{"id":str(r.id),"employeeCode":r.employee_code,"attendanceId":str(r.attendance_id),"workDate":r.date.isoformat(),"workedMinutes":r.minutes,"dailySalary":r.amount,"cumulativeSalary":r.cumulative_salary,"createdAt":iso(r.created_at)} for r in rows],**bal,"totalMinutes":sum(r.minutes for r in rows)}
@router.get('/salary/summary')
def salary_summary(user:User=Depends(admin),db:Session=Depends(get_db)):
    result=[]; totals={"earned":0,"paid":0,"due":0}
    for e in db.scalars(select(Employee).order_by(Employee.last_name,Employee.first_name)):
        b=balance(db,e.employee_code);result.append({"employee":employee_payload(e,db),**b});
        for k in totals:totals[k]+=b[k]
    return {"rows":result,"totals":totals}
@router.post('/payments',status_code=201)
def create_payment(data:PaymentIn,user:User=Depends(admin),db:Session=Depends(get_db)):
    from .store import lock_domain
    lock_domain(db,"salary",data.employeeCode)
    e=db.get(Employee,data.employeeCode)
    if not e:fail(404,"NOT_FOUND","Salarié introuvable.")
    db.scalar(select(Employee.employee_code).where(Employee.employee_code==data.employeeCode).with_for_update())
    b=balance(db,data.employeeCode)
    if data.amount>b['due']:fail(422,"AMOUNT_EXCEEDS_DUE","Le montant dépasse le reste à payer.",{"due":b['due']})
    p=Payment(employee_code=data.employeeCode,amount=data.amount,note=data.note,created_by=user.id);db.add(p);audit(db,user,"PAYMENT_CREATED",data.employeeCode,f"Paiement {data.amount} Ar");db.commit();db.refresh(p);return payment_out(p,e)
def payment_out(p,e):return {"id":str(p.id),"employeeCode":p.employee_code,"employeeName":f"{e.first_name} {e.last_name}" if e else p.employee_code,"amount":p.amount,"note":p.note or "","date":p.created_at.astimezone(TZ).date().isoformat(),"createdAt":iso(p.created_at)}
@router.get('/payments')
def payments(employeeCode:str|None=None,user:User=Depends(admin),db:Session=Depends(get_db)):
    code=employeeCode
    stmt=select(Payment).order_by(Payment.created_at.desc())
    if code:stmt=stmt.where(Payment.employee_code==code)
    return [payment_out(p,db.get(Employee,p.employee_code)) for p in db.scalars(stmt)]
@router.get('/me/payments')
def my_payments(user:User=Depends(employee),db:Session=Depends(get_db)):
    stmt=select(Payment).where(Payment.employee_code==user.employee_code).order_by(Payment.created_at.desc())
    return [payment_out(p,db.get(Employee,p.employee_code)) for p in db.scalars(stmt)]
@router.get('/audit-logs')
def audit_logs(action:str|None=None,from_:str|None=Query(None,alias='from'),to_:str|None=Query(None,alias='to'),limit:int=Query(200,ge=1,le=1000),user:User=Depends(admin),db:Session=Depends(get_db)):
    from .models import AuditLog
    stmt=select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:stmt=stmt.where(AuditLog.action==action)
    if from_:stmt=stmt.where(AuditLog.created_at>=datetime.fromisoformat(from_.replace('Z','+00:00')))
    if to_:stmt=stmt.where(AuditLog.created_at<=datetime.fromisoformat(to_.replace('Z','+00:00')))
    return [{"id":str(x.id),"createdAt":iso(x.created_at),"userLabel":x.user_label,"action":x.action,"targetId":x.target_id,"summary":x.summary} for x in db.scalars(stmt)]
