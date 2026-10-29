from __future__ import annotations
from datetime import date, datetime, timezone, time
from uuid import UUID, uuid4
from pathlib import Path
import io, re, zipfile
from fastapi import APIRouter, Depends, UploadFile, File, Form, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from .core import get_db, current_user, admin, employee, fail, audit, settings, tenant_singleton
from .models import User, Site, SiteMember, Employee, Workplace, EmployeeWorkplace, SiteAttachment, Holiday, CalendarSettings, BusinessLogo, BusinessDocument, DocumentLine, AppSettings
router=APIRouter(tags=['calendar','attachments','business assets','documents'])
MAX_ATTACHMENT=10*1024*1024
ATTACH_TYPES={'.pdf':'application/pdf','.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
def _site_access(db,user,sid):
 s=db.get(Site, UUID(sid)) if re.fullmatch(r'[0-9a-fA-F-]{36}',sid or '') else None
 if not s: fail(404,'NOT_FOUND','Chantier introuvable.')
 if user.role.value!='admin' and not db.get(SiteMember,(s.id,user.employee_code)): fail(403,'FORBIDDEN','Accès réservé aux membres du chantier.')
 return s
def _filetype(ext,data):
 if ext=='.pdf': return data.startswith(b'%PDF-')
 if ext in ('.jpg','.jpeg'): return data.startswith(b'\xff\xd8\xff')
 if ext=='.png': return data.startswith(b'\x89PNG\r\n\x1a\n')
 if ext in ('.xlsx','.docx'):
  try:
   z=zipfile.ZipFile(io.BytesIO(data)); names=z.namelist(); return '[Content_Types].xml' in names and ('xl/workbook.xml' in names if ext=='.xlsx' else 'word/document.xml' in names)
  except Exception:return False
 return False
@router.get('/calendar/holidays')
def holidays(from_:date|None=Query(None,alias='from'),to_:date|None=Query(None,alias='to'),user:User=Depends(current_user),db:Session=Depends(get_db)):
 stmt=select(Holiday).order_by(Holiday.day)
 if from_:stmt=stmt.where(Holiday.day>=from_)
 if to_:stmt=stmt.where(Holiday.day<=to_)
 return [{'id':str(x.id),'name':x.name,'date':x.day.isoformat(),'workday':x.working} for x in db.scalars(stmt).all()]
@router.post('/calendar/holidays',status_code=201)
def add_holiday(body:dict,user:User=Depends(admin),db:Session=Depends(get_db)):
 try: day=date.fromisoformat(body.get('date',body.get('day','')))
 except: fail(400,'VALIDATION_ERROR','Date invalide.')
 name=body.get('name');
 if not isinstance(name,str) or not name.strip() or len(name)>160: fail(400,'VALIDATION_ERROR','Nom invalide.')
 workday=body.get('workday',body.get('working',False))
 if not isinstance(workday,bool):fail(400,'VALIDATION_ERROR','workday doit être booléen.')
 x=Holiday(id=uuid4(),day=day,name=name.strip(),working=workday); db.add(x); db.commit(); return {'id':str(x.id),'date':day.isoformat(),'name':x.name,'workday':x.working}
@router.put('/calendar/holidays/{holiday_id}')
def edit_holiday(holiday_id:str,body:dict,user:User=Depends(admin),db:Session=Depends(get_db)):
 
 try:x=db.get(Holiday,UUID(holiday_id))
 except ValueError:x=None
 if not x: fail(404,'NOT_FOUND','Jour férié introuvable.')
 day_value=body.get('date',body.get('day'))
 if day_value is not None:
  try:x.day=date.fromisoformat(day_value)
  except:fail(400,'VALIDATION_ERROR','Date invalide.')
 if 'name' in body:
  if not isinstance(body['name'],str) or not body['name'].strip() or len(body['name'])>160:fail(400,'VALIDATION_ERROR','Nom invalide.')
  x.name=body['name'].strip()
 working_value=body.get('workday',body.get('working'))
 if working_value is not None:
  if not isinstance(working_value,bool):fail(400,'VALIDATION_ERROR','workday doit être booléen.')
  x.working=working_value
 db.commit();return {'id':str(x.id),'date':x.day.isoformat(),'name':x.name,'workday':x.working}
@router.delete('/calendar/holidays/{holiday_id}',status_code=204)
def delete_holiday(holiday_id:str,user:User=Depends(admin),db:Session=Depends(get_db)):
 
 try:x=db.get(Holiday,UUID(holiday_id))
 except ValueError:x=None
 if not x:fail(404,'NOT_FOUND','Jour férié introuvable.')
 db.delete(x);db.commit()
@router.get('/calendar/settings')
def get_calendar_settings(user:User=Depends(current_user),db:Session=Depends(get_db)):
 x=tenant_singleton(db,CalendarSettings,1); cfg=tenant_singleton(db,AppSettings,1)
 working=x.working_rules if x and isinstance(x.working_rules,dict) else {}
 hours=x.hours_rules if x and isinstance(x.hours_rules,dict) else {}
 return {'workDays':working.get('workDays',[1,2,3,4,5]),'startTime':hours.get('startTime',cfg.normal_entry_time if cfg else '08:00'),'endTime':hours.get('endTime','17:00'),'toleranceMinutes':hours.get('toleranceMinutes',cfg.tolerance_minutes if cfg else 10)}
@router.put('/calendar/settings')
def put_calendar_settings(body:dict,user:User=Depends(admin),db:Session=Depends(get_db)):
 days=body.get('workDays',[1,2,3,4,5]); start=body.get('startTime','08:00'); end=body.get('endTime','17:00'); tolerance=body.get('toleranceMinutes',10)
 if not isinstance(days,list) or any(type(d) is not int or d<0 or d>6 for d in days) or len(set(days))!=len(days):fail(400,'VALIDATION_ERROR','workDays doit contenir des jours uniques de 0 à 6.')
 try: time.fromisoformat(start); time.fromisoformat(end)
 except (ValueError,TypeError):fail(400,'VALIDATION_ERROR','Les horaires sont invalides.')
 if type(tolerance) is not int or tolerance<0 or tolerance>180:fail(400,'VALIDATION_ERROR','La tolérance doit être comprise entre 0 et 180 minutes.')
 x=tenant_singleton(db,CalendarSettings,1) or CalendarSettings(id=1); x.working_rules={'workDays':days}; x.hours_rules={'startTime':start,'endTime':end,'toleranceMinutes':tolerance}; db.add(x)
 cfg=tenant_singleton(db,AppSettings,1) or AppSettings(id=1); cfg.normal_entry_time=start; cfg.tolerance_minutes=tolerance; db.add(cfg)
 db.commit();return {'workDays':days,'startTime':start,'endTime':end,'toleranceMinutes':tolerance}
@router.get('/sites/{site_id}/attachments')
def attachments(site_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
 s=_site_access(db,user,site_id);return [{'id':str(x.id),'name':x.filename,'type':x.attachment_type,'mimeType':x.content_type,'size':x.size,'createdAt':x.created_at.isoformat(),'uploadedBy':str(x.uploaded_by) if x.uploaded_by else None} for x in db.scalars(select(SiteAttachment).where(SiteAttachment.site_id==s.id,SiteAttachment.deleted_at.is_(None))).all()]
@router.post('/sites/{site_id}/attachments',status_code=201)
async def upload(site_id:str,file:UploadFile=File(...),attachment_type:str=Form(default='Autre',alias='type'),user:User=Depends(current_user),db:Session=Depends(get_db)):
 s=_site_access(db,user,site_id); name=Path(file.filename or '').name
 if not name or len(name)>255 or name in ('.','..'):fail(400,'VALIDATION_ERROR','Nom de fichier invalide.')
 if not isinstance(attachment_type,str) or not attachment_type.strip() or len(attachment_type)>50:fail(400,'VALIDATION_ERROR','Catégorie de fichier invalide.')
 ext=Path(name).suffix.lower(); data=await file.read(MAX_ATTACHMENT+1)
 if len(data)>MAX_ATTACHMENT:fail(413,'FILE_TOO_LARGE','Taille maximale: 10 MiB.')
 mime=ATTACH_TYPES.get(ext)
 if not mime or file.content_type!=mime or not _filetype(ext,data):fail(400,'INVALID_FILE_TYPE','Extension, MIME ou signature de fichier invalide.')
 x=SiteAttachment(id=uuid4(),site_id=s.id,filename=name,attachment_type=attachment_type.strip(),content_type=mime,size=len(data),data=data,uploaded_by=user.id);db.add(x);db.commit();return {'id':str(x.id),'name':name,'type':x.attachment_type,'mimeType':mime,'size':len(data),'createdAt':x.created_at.isoformat(),'uploadedBy':str(x.uploaded_by) if x.uploaded_by else None}
def _attachment(db,user,sid,aid):
 s=_site_access(db,user,sid)
 try:x=db.get(SiteAttachment,UUID(aid))
 except:x=None
 if not x or x.site_id!=s.id or x.deleted_at:fail(404,'NOT_FOUND','Pièce jointe introuvable.')
 return x
@router.get('/sites/{site_id}/attachments/{attachment_id}')
def download(site_id:str,attachment_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
 x=_attachment(db,user,site_id,attachment_id);return Response(x.data,media_type=x.content_type,headers={'Content-Disposition':f'attachment; filename="{x.filename.replace(chr(34),"")}"'})
@router.delete('/sites/{site_id}/attachments/{attachment_id}',status_code=204)
def delete_attachment(site_id:str,attachment_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
 x=_attachment(db,user,site_id,attachment_id)
 if user.role.value!='admin' and x.uploaded_by!=user.id:fail(403,'FORBIDDEN','Seul l’auteur ou un administrateur peut supprimer cette pièce jointe.')
 audit(db,user,'SITE_ATTACHMENT_DELETED',x.id,f'Pièce jointe {x.filename} supprimée');x.deleted_at=datetime.now(timezone.utc);db.commit()
@router.get('/business/logo')
def get_logo(user:User=Depends(current_user),db:Session=Depends(get_db)):
 x=tenant_singleton(db,BusinessLogo,1)
 if not x:fail(404,'NOT_FOUND','Aucun logo enregistré.')
 return Response(x.data,media_type=x.content_type,headers={'Cache-Control':'private, max-age=300'})
@router.put('/business/logo')
async def put_logo(file:UploadFile=File(...),user:User=Depends(admin),db:Session=Depends(get_db)):
 data=await file.read(2*1024*1024+1); mime=file.content_type; sig={'image/png':b'\x89PNG\r\n\x1a\n','image/jpeg':b'\xff\xd8\xff'}
 if len(data)>2*1024*1024 or mime not in sig or not data.startswith(sig[mime]):fail(400,'INVALID_FILE_TYPE','Logo invalide (PNG ou JPEG, 2 MiB maximum).')
 x=tenant_singleton(db,BusinessLogo,1) or BusinessLogo(id=1);x.data=data;x.content_type=mime;db.add(x);db.commit();return {'content_type':mime,'size':len(data)}
@router.get('/documents/{document_id}/pdf')
def document_pdf(document_id:str,user:User=Depends(admin),db:Session=Depends(get_db)):
 try:
  from reportlab.lib.pagesizes import A4
  from reportlab.pdfgen import canvas
  from reportlab.lib.utils import ImageReader
 except ImportError:fail(503,'PDF_UNAVAILABLE','ReportLab indisponible.')
 try: doc=db.get(BusinessDocument,UUID(document_id))
 except:doc=None
 if not doc:fail(404,'NOT_FOUND','Document introuvable.')
 lines=db.scalars(select(DocumentLine).where(DocumentLine.document_id==doc.id).order_by(DocumentLine.position)).all(); buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=A4);w,h=A4;y=h-48
 logo=tenant_singleton(db,BusinessLogo,1)
 if logo:
  try:c.drawImage(ImageReader(io.BytesIO(logo.data)),40,y-45,width=110,height=45,preserveAspectRatio=True,anchor='c',mask='auto')
  except Exception:pass
 st=tenant_singleton(db,AppSettings,1); c.setFont('Helvetica-Bold',16);c.drawString(40,y-65,(st.company_name or '') if st else ''); c.setFont('Helvetica',9);c.drawString(40,y-80,(st.company_address or '') if st else '');c.drawString(40,y-94,(st.company_phone or '') if st else '')
 if st:
  c.setFont('Helvetica',8);c.drawString(40,y-108,'  '.join(x for x in (st.company_email or '',st.company_nif or '',st.company_stat or '') if x))
 c.setFont('Helvetica-Bold',15);c.drawRightString(w-40,y-60,f'{doc.document_type} {doc.number}');c.setFont('Helvetica',10);c.drawRightString(w-40,y-78,f'Date: {doc.issue_date.isoformat()}')
 client=doc.client_snapshot or {}; y-=130;c.setFont('Helvetica-Bold',11);c.drawString(40,y,'Client: '+str(client.get('name') or ''));y-=18;c.setFont('Helvetica',9)
 for k in ('address','phone','email','nif','stat'):
  if client.get(k):c.drawString(40,y,str(client[k])[:110]);y-=13
 y-=20;c.setFont('Helvetica-Bold',9)
 for xx,t in ((40,'Désignation'),(320,'Qté'),(365,'Unité'),(410,'Prix unitaire'),(510,'Montant')):c.drawString(xx,y,t)
 y-=15;c.setFont('Helvetica',9)
 for ln in lines:
  if y<90:c.showPage();y=h-50;c.setFont('Helvetica',9)
  c.drawString(40,y,str(ln.description)[:48]);c.drawRightString(350,y,str(ln.quantity));c.drawString(365,y,str(ln.unit or '')[:10]);c.drawRightString(475,y,str(ln.unit_price));c.drawRightString(w-40,y,str(ln.amount));y-=16
 y-=10;c.drawRightString(w-40,y,f'Sous-total: {doc.subtotal}');y-=15;c.drawRightString(w-40,y,f'Taxe ({doc.tax_rate}%): {doc.tax_amount}');y-=15;c.setFont('Helvetica-Bold',11);c.drawRightString(w-40,y,f'Total: {doc.total}');y-=28;c.setFont('Helvetica',9)
 for txt in (doc.subject,doc.notes,st.document_footer if st else ''):
  if txt:c.drawString(40,y,str(txt)[:120]);y-=14
 c.save();return Response(buf.getvalue(),media_type='application/pdf',headers={'Content-Disposition':f'inline; filename="{doc.number}.pdf"'})


# from __future__ import annotations
# from datetime import date, datetime, timezone, time
# from uuid import UUID, uuid4
# from pathlib import Path
# import io, re, zipfile
# from fastapi import APIRouter, Depends, UploadFile, File, Form, Query
# from fastapi.responses import Response
# from sqlalchemy import select
# from sqlalchemy.orm import Session
# from .core import get_db, current_user, admin, employee, fail, audit, settings
# from .models import User, Site, SiteMember, Employee, Workplace, EmployeeWorkplace, SiteAttachment, Holiday, CalendarSettings, BusinessLogo, BusinessDocument, DocumentLine, AppSettings
# router=APIRouter(tags=['calendar','attachments','business assets','documents'])
# MAX_ATTACHMENT=10*1024*1024
# ATTACH_TYPES={'.pdf':'application/pdf','.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
# def _site_access(db,user,sid):
#  s=db.get(Site, UUID(sid)) if re.fullmatch(r'[0-9a-fA-F-]{36}',sid or '') else None
#  if not s: fail(404,'NOT_FOUND','Chantier introuvable.')
#  if user.role.value!='admin' and not db.get(SiteMember,(s.id,user.employee_code)): fail(403,'FORBIDDEN','Accès réservé aux membres du chantier.')
#  return s
# def _filetype(ext,data):
#  if ext=='.pdf': return data.startswith(b'%PDF-')
#  if ext in ('.jpg','.jpeg'): return data.startswith(b'\xff\xd8\xff')
#  if ext=='.png': return data.startswith(b'\x89PNG\r\n\x1a\n')
#  if ext in ('.xlsx','.docx'):
#   try:
#    z=zipfile.ZipFile(io.BytesIO(data)); names=z.namelist(); return '[Content_Types].xml' in names and ('xl/workbook.xml' in names if ext=='.xlsx' else 'word/document.xml' in names)
#   except Exception:return False
#  return False
# @router.get('/calendar/holidays')
# def holidays(from_:date|None=Query(None,alias='from'),to_:date|None=Query(None,alias='to'),user:User=Depends(current_user),db:Session=Depends(get_db)):
#  stmt=select(Holiday).order_by(Holiday.day)
#  if from_:stmt=stmt.where(Holiday.day>=from_)
#  if to_:stmt=stmt.where(Holiday.day<=to_)
#  return [{'id':str(x.id),'name':x.name,'date':x.day.isoformat(),'workday':x.working} for x in db.scalars(stmt).all()]
# @router.post('/calendar/holidays',status_code=201)
# def add_holiday(body:dict,user:User=Depends(admin),db:Session=Depends(get_db)):
#  try: day=date.fromisoformat(body.get('date',body.get('day','')))
#  except: fail(400,'VALIDATION_ERROR','Date invalide.')
#  name=body.get('name');
#  if not isinstance(name,str) or not name.strip() or len(name)>160: fail(400,'VALIDATION_ERROR','Nom invalide.')
#  workday=body.get('workday',body.get('working',False))
#  if not isinstance(workday,bool):fail(400,'VALIDATION_ERROR','workday doit être booléen.')
#  x=Holiday(id=uuid4(),day=day,name=name.strip(),working=workday); db.add(x); db.commit(); return {'id':str(x.id),'date':day.isoformat(),'name':x.name,'workday':x.working}
# @router.put('/calendar/holidays/{holiday_id}')
# def edit_holiday(holiday_id:str,body:dict,user:User=Depends(admin),db:Session=Depends(get_db)):
 
#  try:x=db.get(Holiday,UUID(holiday_id))
#  except ValueError:x=None
#  if not x: fail(404,'NOT_FOUND','Jour férié introuvable.')
#  day_value=body.get('date',body.get('day'))
#  if day_value is not None:
#   try:x.day=date.fromisoformat(day_value)
#   except:fail(400,'VALIDATION_ERROR','Date invalide.')
#  if 'name' in body:
#   if not isinstance(body['name'],str) or not body['name'].strip() or len(body['name'])>160:fail(400,'VALIDATION_ERROR','Nom invalide.')
#   x.name=body['name'].strip()
#  working_value=body.get('workday',body.get('working'))
#  if working_value is not None:
#   if not isinstance(working_value,bool):fail(400,'VALIDATION_ERROR','workday doit être booléen.')
#   x.working=working_value
#  db.commit();return {'id':str(x.id),'date':x.day.isoformat(),'name':x.name,'workday':x.working}
# @router.delete('/calendar/holidays/{holiday_id}',status_code=204)
# def delete_holiday(holiday_id:str,user:User=Depends(admin),db:Session=Depends(get_db)):
 
#  try:x=db.get(Holiday,UUID(holiday_id))
#  except ValueError:x=None
#  if not x:fail(404,'NOT_FOUND','Jour férié introuvable.')
#  db.delete(x);db.commit()
# @router.get('/calendar/settings')
# def get_calendar_settings(user:User=Depends(current_user),db:Session=Depends(get_db)):
#  x=db.get(CalendarSettings,1); cfg=db.get(AppSettings,1)
#  working=x.working_rules if x and isinstance(x.working_rules,dict) else {}
#  hours=x.hours_rules if x and isinstance(x.hours_rules,dict) else {}
#  return {'workDays':working.get('workDays',[1,2,3,4,5]),'startTime':hours.get('startTime',cfg.normal_entry_time if cfg else '08:00'),'endTime':hours.get('endTime','17:00'),'toleranceMinutes':hours.get('toleranceMinutes',cfg.tolerance_minutes if cfg else 10)}
# @router.put('/calendar/settings')
# def put_calendar_settings(body:dict,user:User=Depends(admin),db:Session=Depends(get_db)):
#  days=body.get('workDays',[1,2,3,4,5]); start=body.get('startTime','08:00'); end=body.get('endTime','17:00'); tolerance=body.get('toleranceMinutes',10)
#  if not isinstance(days,list) or any(type(d) is not int or d<0 or d>6 for d in days) or len(set(days))!=len(days):fail(400,'VALIDATION_ERROR','workDays doit contenir des jours uniques de 0 à 6.')
#  try: time.fromisoformat(start); time.fromisoformat(end)
#  except (ValueError,TypeError):fail(400,'VALIDATION_ERROR','Les horaires sont invalides.')
#  if type(tolerance) is not int or tolerance<0 or tolerance>180:fail(400,'VALIDATION_ERROR','La tolérance doit être comprise entre 0 et 180 minutes.')
#  x=db.get(CalendarSettings,1) or CalendarSettings(id=1); x.working_rules={'workDays':days}; x.hours_rules={'startTime':start,'endTime':end,'toleranceMinutes':tolerance}; db.add(x)
#  cfg=db.get(AppSettings,1) or AppSettings(id=1); cfg.normal_entry_time=start; cfg.tolerance_minutes=tolerance; db.add(cfg)
#  db.commit();return {'workDays':days,'startTime':start,'endTime':end,'toleranceMinutes':tolerance}
# @router.get('/sites/{site_id}/attachments')
# def attachments(site_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
#  s=_site_access(db,user,site_id);return [{'id':str(x.id),'name':x.filename,'type':x.attachment_type,'mimeType':x.content_type,'size':x.size,'createdAt':x.created_at.isoformat(),'uploadedBy':str(x.uploaded_by) if x.uploaded_by else None} for x in db.scalars(select(SiteAttachment).where(SiteAttachment.site_id==s.id,SiteAttachment.deleted_at.is_(None))).all()]
# @router.post('/sites/{site_id}/attachments',status_code=201)
# async def upload(site_id:str,file:UploadFile=File(...),attachment_type:str=Form(default='Autre',alias='type'),user:User=Depends(current_user),db:Session=Depends(get_db)):
#  s=_site_access(db,user,site_id); name=Path(file.filename or '').name
#  if not name or len(name)>255 or name in ('.','..'):fail(400,'VALIDATION_ERROR','Nom de fichier invalide.')
#  if not isinstance(attachment_type,str) or not attachment_type.strip() or len(attachment_type)>50:fail(400,'VALIDATION_ERROR','Catégorie de fichier invalide.')
#  ext=Path(name).suffix.lower(); data=await file.read(MAX_ATTACHMENT+1)
#  if len(data)>MAX_ATTACHMENT:fail(413,'FILE_TOO_LARGE','Taille maximale: 10 MiB.')
#  mime=ATTACH_TYPES.get(ext)
#  if not mime or file.content_type!=mime or not _filetype(ext,data):fail(400,'INVALID_FILE_TYPE','Extension, MIME ou signature de fichier invalide.')
#  x=SiteAttachment(id=uuid4(),site_id=s.id,filename=name,attachment_type=attachment_type.strip(),content_type=mime,size=len(data),data=data,uploaded_by=user.id);db.add(x);db.commit();return {'id':str(x.id),'name':name,'type':x.attachment_type,'mimeType':mime,'size':len(data),'createdAt':x.created_at.isoformat(),'uploadedBy':str(x.uploaded_by) if x.uploaded_by else None}
# def _attachment(db,user,sid,aid):
#  s=_site_access(db,user,sid)
#  try:x=db.get(SiteAttachment,UUID(aid))
#  except:x=None
#  if not x or x.site_id!=s.id or x.deleted_at:fail(404,'NOT_FOUND','Pièce jointe introuvable.')
#  return x
# @router.get('/sites/{site_id}/attachments/{attachment_id}')
# def download(site_id:str,attachment_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
#  x=_attachment(db,user,site_id,attachment_id);return Response(x.data,media_type=x.content_type,headers={'Content-Disposition':f'attachment; filename="{x.filename.replace(chr(34),"")}"'})
# @router.delete('/sites/{site_id}/attachments/{attachment_id}',status_code=204)
# def delete_attachment(site_id:str,attachment_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
#  x=_attachment(db,user,site_id,attachment_id)
#  if user.role.value!='admin' and x.uploaded_by!=user.id:fail(403,'FORBIDDEN','Seul l’auteur ou un administrateur peut supprimer cette pièce jointe.')
#  audit(db,user,'SITE_ATTACHMENT_DELETED',x.id,f'Pièce jointe {x.filename} supprimée');x.deleted_at=datetime.now(timezone.utc);db.commit()
# @router.get('/business/logo')
# def get_logo(user:User=Depends(current_user),db:Session=Depends(get_db)):
#  x=db.get(BusinessLogo,1)
#  if not x:fail(404,'NOT_FOUND','Aucun logo enregistré.')
#  return Response(x.data,media_type=x.content_type,headers={'Cache-Control':'private, max-age=300'})
# @router.put('/business/logo')
# async def put_logo(file:UploadFile=File(...),user:User=Depends(admin),db:Session=Depends(get_db)):
#  data=await file.read(2*1024*1024+1); mime=file.content_type; sig={'image/png':b'\x89PNG\r\n\x1a\n','image/jpeg':b'\xff\xd8\xff'}
#  if len(data)>2*1024*1024 or mime not in sig or not data.startswith(sig[mime]):fail(400,'INVALID_FILE_TYPE','Logo invalide (PNG ou JPEG, 2 MiB maximum).')
#  x=db.get(BusinessLogo,1) or BusinessLogo(id=1);x.data=data;x.content_type=mime;db.add(x);db.commit();return {'content_type':mime,'size':len(data)}
# @router.get('/documents/{document_id}/pdf')
# def document_pdf(document_id:str,user:User=Depends(admin),db:Session=Depends(get_db)):
#  try:
#   from reportlab.lib.pagesizes import A4
#   from reportlab.pdfgen import canvas
#   from reportlab.lib.utils import ImageReader
#  except ImportError:fail(503,'PDF_UNAVAILABLE','ReportLab indisponible.')
#  try: doc=db.get(BusinessDocument,UUID(document_id))
#  except:doc=None
#  if not doc:fail(404,'NOT_FOUND','Document introuvable.')
#  lines=db.scalars(select(DocumentLine).where(DocumentLine.document_id==doc.id).order_by(DocumentLine.position)).all(); buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=A4);w,h=A4;y=h-48
#  logo=db.get(BusinessLogo,1)
#  if logo:
#   try:c.drawImage(ImageReader(io.BytesIO(logo.data)),40,y-45,width=110,height=45,preserveAspectRatio=True,anchor='c',mask='auto')
#   except Exception:pass
#  st=db.get(AppSettings,1); c.setFont('Helvetica-Bold',16);c.drawString(40,y-65,(st.company_name or '') if st else ''); c.setFont('Helvetica',9);c.drawString(40,y-80,(st.company_address or '') if st else '');c.drawString(40,y-94,(st.company_phone or '') if st else '')
#  if st:
#   c.setFont('Helvetica',8);c.drawString(40,y-108,'  '.join(x for x in (st.company_email or '',st.company_nif or '',st.company_stat or '') if x))
#  c.setFont('Helvetica-Bold',15);c.drawRightString(w-40,y-60,f'{doc.document_type} {doc.number}');c.setFont('Helvetica',10);c.drawRightString(w-40,y-78,f'Date: {doc.issue_date.isoformat()}')
#  client=doc.client_snapshot or {}; y-=130;c.setFont('Helvetica-Bold',11);c.drawString(40,y,'Client: '+str(client.get('name') or ''));y-=18;c.setFont('Helvetica',9)
#  for k in ('address','phone','email','nif','stat'):
#   if client.get(k):c.drawString(40,y,str(client[k])[:110]);y-=13
#  y-=20;c.setFont('Helvetica-Bold',9)
#  for xx,t in ((40,'Désignation'),(320,'Qté'),(365,'Unité'),(410,'Prix unitaire'),(510,'Montant')):c.drawString(xx,y,t)
#  y-=15;c.setFont('Helvetica',9)
#  for ln in lines:
#   if y<90:c.showPage();y=h-50;c.setFont('Helvetica',9)
#   c.drawString(40,y,str(ln.description)[:48]);c.drawRightString(350,y,str(ln.quantity));c.drawString(365,y,str(ln.unit or '')[:10]);c.drawRightString(475,y,str(ln.unit_price));c.drawRightString(w-40,y,str(ln.amount));y-=16
#  y-=10;c.drawRightString(w-40,y,f'Sous-total: {doc.subtotal}');y-=15;c.drawRightString(w-40,y,f'Taxe ({doc.tax_rate}%): {doc.tax_amount}');y-=15;c.setFont('Helvetica-Bold',11);c.drawRightString(w-40,y,f'Total: {doc.total}');y-=28;c.setFont('Helvetica',9)
#  for txt in (doc.subject,doc.notes,st.document_footer if st else ''):
#   if txt:c.drawString(40,y,str(txt)[:120]);y-=14
#  c.save();return Response(buf.getvalue(),media_type='application/pdf',headers={'Content-Disposition':f'inline; filename="{doc.number}.pdf"'})
