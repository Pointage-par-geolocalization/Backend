from __future__ import annotations
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
import csv, io
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, delete
from sqlalchemy.orm import Session
from .core import get_db, admin, fail, verify_password, audit, iso, settings, tenant_singleton
from .models import User, Role, Employee, Attendance, AttendanceStatus, SalaryRecord, Payment, EmployeeWorkplace, AppSettings, EquipmentLoan, Equipment, LoanStatus, Chat, utcnow
from .api import day_bounds, balance
from .store import rows, remove, emit_after_commit, lock_domain
router=APIRouter(tags=["administration"])
TZ=ZoneInfo(settings.timezone_name)

def _today_data(db):
    today,start,end=day_bounds(); active=db.scalars(select(Employee).where(Employee.active.is_(True)).order_by(Employee.last_name,Employee.first_name)).all()
    ats=db.scalars(select(Attendance).where(Attendance.entry_time>=start,Attendance.entry_time<end).order_by(Attendance.entry_time)).all()
    bycode={}
    for a in ats: bycode.setdefault(a.employee_code,[]).append(a)
    leaves=rows(db,"leaves"); approved=[]
    for l in leaves:
        if l.get("status")=="ACCEPTEE" and l.get("startDate","9999-99-99")<=today.isoformat()<=l.get("endDate",""):
            approved.append(l)
    leave_codes={l.get("employeeCode") for l in approved}
    def ref(e): return {"employeeCode":e.employee_code,"name":f"{e.first_name} {e.last_name}"}
    absent=[]; on_leave=[]
    for e in active:
        if bycode.get(e.employee_code): continue
        lv=next((x for x in approved if x.get("employeeCode")==e.employee_code),None)
        if lv: on_leave.append({**ref(e),"type":lv.get("type")})
        else: absent.append(ref(e))
    minutes={code:sum(a.worked_minutes for a in arr) for code,arr in bycode.items()}
    daily_pay=sum(a.daily_salary for a in ats)
    totals={"earned":0,"paid":0,"due":0}
    for e in active:
        b=balance(db,e.employee_code)
        for k in totals: totals[k]+=b[k]
    top=[]
    empmap={e.employee_code:e for e in active}
    for code,m in sorted(minutes.items(),key=lambda x:(-x[1],x[0])):
        if m>0 and code in empmap: top.append({**ref(empmap[code]),"minutes":m})
    top=top[:5]
    cfg=tenant_singleton(db,AppSettings,1); entry=(cfg.normal_entry_time if cfg else "08:00") or "08:00"; tolerance=cfg.tolerance_minutes if cfg else 10
    try: entry_clock=datetime.strptime(entry,"%H:%M").time()
    except ValueError: entry_clock=time(8,0)
    late=[]; current=[]
    for code,arr in bycode.items():
        emp=db.get(Employee,code)
        if not emp: continue
        opens=[a for a in arr if a.status==AttendanceStatus.EN_COURS]
        for a in opens:
            current.append({**ref(emp),"attendanceId":str(a.id),"entryTime":iso(a.entry_time),"departmentName":a.department_name,"siteName":None})
        first=min(arr,key=lambda a:a.entry_time)
        local=first.entry_time.astimezone(TZ); scheduled=datetime.combine(today,entry_clock,tzinfo=TZ)+timedelta(minutes=tolerance)
        delta=int((local-scheduled).total_seconds()//60)
        if delta>0: late.append({**ref(emp),"lateMinutes":delta,"entryTime":iso(first.entry_time)})
    current.sort(key=lambda x:x["entryTime"]);late.sort(key=lambda x:-x["lateMinutes"])
    pending_loans=db.scalar(select(func.count()).select_from(EquipmentLoan).where(EquipmentLoan.status.in_([LoanStatus.DEMANDE,LoanStatus.RETOUR_DEMANDE]))) or 0
    sites=rows(db,"sites");active_sites=sum(1 for s in sites if s.get("status")=="EN_COURS")
    rounds=rows(db,"rounds"); today_rounds=[r for r in rounds if str(r.get("startedAt",""))[:10]==today.isoformat()]
    today_rounds.sort(key=lambda r:r.get("startedAt", ""),reverse=True);last=today_rounds[0] if today_rounds else None
    checks=rows(db,"checks"); anomalies=[c for c in checks if c.get("status") in ("HORS_ZONE","SANS_REPONSE") and str(c.get("checkedAt") or "")[:10]==today.isoformat()]
    anomalies.sort(key=lambda c:c.get("checkedAt") or "",reverse=True)
    counts={"present":0,"outOfZone":0,"noResponse":0}
    if last:
        counts={"present":last.get("present",0),"outOfZone":last.get("outOfZone",0),"noResponse":last.get("noResponse",0)}
    last_round={**last,**counts} if last else None
    return {"date":today.isoformat(),"activeEmployees":len(active),"presentCount":len(bycode),"absent":absent,"onLeave":on_leave,"totalMinutes":sum(minutes.values()),"dailyPay":daily_pay,"cashDue":totals["due"],"topWorked":top,"currentlyIn":current,"late":late,"pendingLeaves":sum(1 for l in leaves if l.get("status")=="EN_ATTENTE"),"pendingLoans":pending_loans,"activeSites":active_sites,"presence":{"lastRound":last_round,"anomalies":anomalies}}

@router.get('/dashboard/today')
def dashboard_today(user:User=Depends(admin),db:Session=Depends(get_db)):
    return _today_data(db)

@router.get('/reports/salary')
def salary_report(from_:str=Query(...,alias='from'),to_:str=Query(...,alias='to'),siteId:str|None=None,format_:str=Query('json',alias='format'),user:User=Depends(admin),db:Session=Depends(get_db)):
    try: start=date.fromisoformat(from_); end=date.fromisoformat(to_)
    except ValueError: fail(400,"VALIDATION_ERROR","Les dates du rapport sont invalides.")
    if end<start: fail(400,"VALIDATION_ERROR","La date de fin doit être postérieure à la date de début.")
    stmt=select(SalaryRecord).where(SalaryRecord.date>=start,SalaryRecord.date<=end)
    recs=list(db.scalars(stmt))
    if siteId:
        ids={str(a.id) for a in db.scalars(select(Attendance).where(Attendance.site_id==__import__('uuid').UUID(siteId)))}
        recs=[r for r in recs if str(r.attendance_id) in ids]
    grouped={}
    for r in recs:
        g=grouped.setdefault(r.employee_code,{"days":set(),"minutes":0,"total":0})
        g["days"].add(r.date.isoformat());g["minutes"]+=r.minutes;g["total"]+=r.amount
    rows_out=[]
    for code,g in grouped.items():
        e=db.get(Employee,code)
        if e: rows_out.append({"employeeCode":code,"name":f"{e.first_name} {e.last_name}","days":len(g["days"]),"minutes":g["minutes"],"total":g["total"]})
    rows_out.sort(key=lambda x:(-x["total"],x["name"].casefold()))
    total=sum(x["total"] for x in rows_out)
    if format_=="csv":
        stream=io.StringIO();w=csv.writer(stream);w.writerow(["Code salarié","Nom","Jours","Minutes","Total (Ar)"])
        for r in rows_out:w.writerow([r["employeeCode"],r["name"],r["days"],r["minutes"],r["total"]])
        w.writerow(["","","","Total général",total]); data=stream.getvalue().encode('utf-8-sig')
        return StreamingResponse(io.BytesIO(data),media_type='text/csv; charset=utf-8',headers={"Content-Disposition":"attachment; filename=rapport-salaires.csv"})
    return {"rows":rows_out,"total":total}

@router.post('/admin/reset-data',status_code=204)
def reset_data(body:dict,user:User=Depends(admin),db:Session=Depends(get_db)):
    if not verify_password(str(body.get("password","")),user.password_hash): fail(403,"INVALID_PASSWORD","Mot de passe incorrect.")
    from .models import RefreshToken
    from .store import DomainRecord
    from sqlalchemy import delete as sql_delete
    employee_users=list(db.scalars(select(User).where(User.role==Role.employee)))
    revoked={str(x.id) for x in employee_users}
    # Un seul commit: tout le reset est annulé si une des suppressions échoue.
    lock_domain(db,"admin-reset","all")
    db.execute(sql_delete(RefreshToken).where(RefreshToken.user_id.in_([x.id for x in employee_users])))
    db.execute(sql_delete(Payment))
    db.execute(sql_delete(SalaryRecord))
    db.execute(sql_delete(Attendance))
    db.execute(sql_delete(EquipmentLoan))
    db.execute(sql_delete(Equipment))
    db.execute(sql_delete(Chat))
    db.execute(sql_delete(EmployeeWorkplace))
    db.execute(sql_delete(User).where(User.role==Role.employee))
    db.execute(sql_delete(Employee))
    db.execute(sql_delete(DomainRecord).where(DomainRecord.domain.in_([
        "equipment","loans","chats","messages","reads","leaves","rounds","checks","site_members"
    ])))
    audit(db,user,"DATA_RESET",None,"Réinitialisation des données salariés")
    db.commit()
    emit_after_commit(db,{"type":"session.revoked"},revoked)


# from __future__ import annotations
# from datetime import datetime, date, time, timedelta, timezone
# from zoneinfo import ZoneInfo
# import csv, io
# from fastapi import APIRouter, Depends, Query
# from fastapi.responses import StreamingResponse
# from sqlalchemy import select, func, delete
# from sqlalchemy.orm import Session
# from .core import get_db, admin, fail, verify_password, audit, iso, settings
# from .models import User, Role, Employee, Attendance, AttendanceStatus, SalaryRecord, Payment, EmployeeWorkplace, AppSettings, EquipmentLoan, Equipment, LoanStatus, Chat, utcnow
# from .api import day_bounds, balance
# from .store import rows, remove, emit_after_commit, lock_domain
# router=APIRouter(tags=["administration"])
# TZ=ZoneInfo(settings.timezone_name)

# def _today_data(db):
#     today,start,end=day_bounds(); active=db.scalars(select(Employee).where(Employee.active.is_(True)).order_by(Employee.last_name,Employee.first_name)).all()
#     ats=db.scalars(select(Attendance).where(Attendance.entry_time>=start,Attendance.entry_time<end).order_by(Attendance.entry_time)).all()
#     bycode={}
#     for a in ats: bycode.setdefault(a.employee_code,[]).append(a)
#     leaves=rows(db,"leaves"); approved=[]
#     for l in leaves:
#         if l.get("status")=="ACCEPTEE" and l.get("startDate","9999-99-99")<=today.isoformat()<=l.get("endDate",""):
#             approved.append(l)
#     leave_codes={l.get("employeeCode") for l in approved}
#     def ref(e): return {"employeeCode":e.employee_code,"name":f"{e.first_name} {e.last_name}"}
#     absent=[]; on_leave=[]
#     for e in active:
#         if bycode.get(e.employee_code): continue
#         lv=next((x for x in approved if x.get("employeeCode")==e.employee_code),None)
#         if lv: on_leave.append({**ref(e),"type":lv.get("type")})
#         else: absent.append(ref(e))
#     minutes={code:sum(a.worked_minutes for a in arr) for code,arr in bycode.items()}
#     daily_pay=sum(a.daily_salary for a in ats)
#     totals={"earned":0,"paid":0,"due":0}
#     for e in active:
#         b=balance(db,e.employee_code)
#         for k in totals: totals[k]+=b[k]
#     top=[]
#     empmap={e.employee_code:e for e in active}
#     for code,m in sorted(minutes.items(),key=lambda x:(-x[1],x[0])):
#         if m>0 and code in empmap: top.append({**ref(empmap[code]),"minutes":m})
#     top=top[:5]
#     cfg=db.get(AppSettings,1); entry=(cfg.normal_entry_time if cfg else "08:00") or "08:00"; tolerance=cfg.tolerance_minutes if cfg else 10
#     try: entry_clock=datetime.strptime(entry,"%H:%M").time()
#     except ValueError: entry_clock=time(8,0)
#     late=[]; current=[]
#     for code,arr in bycode.items():
#         emp=db.get(Employee,code)
#         if not emp: continue
#         opens=[a for a in arr if a.status==AttendanceStatus.EN_COURS]
#         for a in opens:
#             current.append({**ref(emp),"attendanceId":str(a.id),"entryTime":iso(a.entry_time),"departmentName":a.department_name,"siteName":None})
#         first=min(arr,key=lambda a:a.entry_time)
#         local=first.entry_time.astimezone(TZ); scheduled=datetime.combine(today,entry_clock,tzinfo=TZ)+timedelta(minutes=tolerance)
#         delta=int((local-scheduled).total_seconds()//60)
#         if delta>0: late.append({**ref(emp),"lateMinutes":delta,"entryTime":iso(first.entry_time)})
#     current.sort(key=lambda x:x["entryTime"]);late.sort(key=lambda x:-x["lateMinutes"])
#     pending_loans=db.scalar(select(func.count()).select_from(EquipmentLoan).where(EquipmentLoan.status.in_([LoanStatus.DEMANDE,LoanStatus.RETOUR_DEMANDE]))) or 0
#     sites=rows(db,"sites");active_sites=sum(1 for s in sites if s.get("status")=="EN_COURS")
#     rounds=rows(db,"rounds"); today_rounds=[r for r in rounds if str(r.get("startedAt",""))[:10]==today.isoformat()]
#     today_rounds.sort(key=lambda r:r.get("startedAt", ""),reverse=True);last=today_rounds[0] if today_rounds else None
#     checks=rows(db,"checks"); anomalies=[c for c in checks if c.get("status") in ("HORS_ZONE","SANS_REPONSE") and str(c.get("checkedAt") or "")[:10]==today.isoformat()]
#     anomalies.sort(key=lambda c:c.get("checkedAt") or "",reverse=True)
#     counts={"present":0,"outOfZone":0,"noResponse":0}
#     if last:
#         counts={"present":last.get("present",0),"outOfZone":last.get("outOfZone",0),"noResponse":last.get("noResponse",0)}
#     last_round={**last,**counts} if last else None
#     return {"date":today.isoformat(),"activeEmployees":len(active),"presentCount":len(bycode),"absent":absent,"onLeave":on_leave,"totalMinutes":sum(minutes.values()),"dailyPay":daily_pay,"cashDue":totals["due"],"topWorked":top,"currentlyIn":current,"late":late,"pendingLeaves":sum(1 for l in leaves if l.get("status")=="EN_ATTENTE"),"pendingLoans":pending_loans,"activeSites":active_sites,"presence":{"lastRound":last_round,"anomalies":anomalies}}

# @router.get('/dashboard/today')
# def dashboard_today(user:User=Depends(admin),db:Session=Depends(get_db)):
#     return _today_data(db)

# @router.get('/reports/salary')
# def salary_report(from_:str=Query(...,alias='from'),to_:str=Query(...,alias='to'),siteId:str|None=None,format_:str=Query('json',alias='format'),user:User=Depends(admin),db:Session=Depends(get_db)):
#     try: start=date.fromisoformat(from_); end=date.fromisoformat(to_)
#     except ValueError: fail(400,"VALIDATION_ERROR","Les dates du rapport sont invalides.")
#     if end<start: fail(400,"VALIDATION_ERROR","La date de fin doit être postérieure à la date de début.")
#     stmt=select(SalaryRecord).where(SalaryRecord.date>=start,SalaryRecord.date<=end)
#     recs=list(db.scalars(stmt))
#     if siteId:
#         ids={str(a.id) for a in db.scalars(select(Attendance).where(Attendance.site_id==__import__('uuid').UUID(siteId)))}
#         recs=[r for r in recs if str(r.attendance_id) in ids]
#     grouped={}
#     for r in recs:
#         g=grouped.setdefault(r.employee_code,{"days":set(),"minutes":0,"total":0})
#         g["days"].add(r.date.isoformat());g["minutes"]+=r.minutes;g["total"]+=r.amount
#     rows_out=[]
#     for code,g in grouped.items():
#         e=db.get(Employee,code)
#         if e: rows_out.append({"employeeCode":code,"name":f"{e.first_name} {e.last_name}","days":len(g["days"]),"minutes":g["minutes"],"total":g["total"]})
#     rows_out.sort(key=lambda x:(-x["total"],x["name"].casefold()))
#     total=sum(x["total"] for x in rows_out)
#     if format_=="csv":
#         stream=io.StringIO();w=csv.writer(stream);w.writerow(["Code salarié","Nom","Jours","Minutes","Total (Ar)"])
#         for r in rows_out:w.writerow([r["employeeCode"],r["name"],r["days"],r["minutes"],r["total"]])
#         w.writerow(["","","","Total général",total]); data=stream.getvalue().encode('utf-8-sig')
#         return StreamingResponse(io.BytesIO(data),media_type='text/csv; charset=utf-8',headers={"Content-Disposition":"attachment; filename=rapport-salaires.csv"})
#     return {"rows":rows_out,"total":total}

# @router.post('/admin/reset-data',status_code=204)
# def reset_data(body:dict,user:User=Depends(admin),db:Session=Depends(get_db)):
#     if not verify_password(str(body.get("password","")),user.password_hash): fail(403,"INVALID_PASSWORD","Mot de passe incorrect.")
#     from .models import RefreshToken
#     from .store import DomainRecord
#     from sqlalchemy import delete as sql_delete
#     employee_users=list(db.scalars(select(User).where(User.role==Role.employee)))
#     revoked={str(x.id) for x in employee_users}
#     # Un seul commit: tout le reset est annulé si une des suppressions échoue.
#     lock_domain(db,"admin-reset","all")
#     db.execute(sql_delete(RefreshToken).where(RefreshToken.user_id.in_([x.id for x in employee_users])))
#     db.execute(sql_delete(Payment))
#     db.execute(sql_delete(SalaryRecord))
#     db.execute(sql_delete(Attendance))
#     db.execute(sql_delete(EquipmentLoan))
#     db.execute(sql_delete(Equipment))
#     db.execute(sql_delete(Chat))
#     db.execute(sql_delete(EmployeeWorkplace))
#     db.execute(sql_delete(User).where(User.role==Role.employee))
#     db.execute(sql_delete(Employee))
#     db.execute(sql_delete(DomainRecord).where(DomainRecord.domain.in_([
#         "equipment","loans","chats","messages","reads","leaves","rounds","checks","site_members"
#     ])))
#     audit(db,user,"DATA_RESET",None,"Réinitialisation des données salariés")
#     db.commit()
#     emit_after_commit(db,{"type":"session.revoked"},revoked)
