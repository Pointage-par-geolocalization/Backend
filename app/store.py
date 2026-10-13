from __future__ import annotations
from datetime import datetime, date, timezone
from uuid import UUID, uuid4
from typing import Any
from fastapi import WebSocket
from sqlalchemy import String, DateTime, select, delete, event
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, Session
from .models import (
    Base, Client, Site, SiteMember, SiteStep, SiteNote, BusinessDocument,
    DocumentLine, DocumentVersion, DocumentCounter, Chat, ChatMember,
    ChatMessage, MessageReaction, ChatRead, User,
)

class DomainRecord(Base):
    __tablename__ = "domain_records"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    domain: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

RELATIONAL_DOMAINS = {"clients", "sites", "site_members", "site_steps", "site_notes", "documents", "document_versions", "doc_counters", "chats", "messages", "reads"}

def _uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID): return value
    try: return UUID(str(value)) if value else None
    except (TypeError, ValueError): return None

def _dt(value: Any, default: datetime | None = None) -> datetime | None:
    if value is None: return default
    if isinstance(value, datetime): return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        x = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError): return default

def _day(value: Any) -> date | None:
    if value is None or value == "": return None
    if isinstance(value, date) and not isinstance(value, datetime): return value
    try: return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError): return None

def _iso(value: Any) -> Any:
    if isinstance(value, datetime): return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, date): return value.isoformat()
    return value

def _client_view(x: Client) -> dict:
    return {"id": str(x.id), "name": x.name, "contactName": x.contact_name, "phone": x.phone, "email": x.email, "address": x.address, "nif": x.nif, "stat": x.stat, "notes": x.notes}

def _site_view(x: Site) -> dict:
    return {"id": str(x.id), "name": x.name, "clientId": str(x.client_id) if x.client_id else None, "clientSnapshot": x.client_snapshot, "workplaceId": str(x.workplace_id) if x.workplace_id else None, "address": x.address, "description": x.description, "startDate": _iso(x.start_date), "endDate": _iso(x.end_date), "status": x.status, "manualProgress": x.manual_progress, "createdAt": _iso(x.created_at)}

def _member_view(x: SiteMember) -> dict: return {"id": f"{x.site_id}:{x.employee_code}", "siteId": str(x.site_id), "employeeCode": x.employee_code}
def _step_view(x: SiteStep) -> dict: return {"id": str(x.id), "siteId": str(x.site_id), "title": x.title, "description": x.description, "position": x.position, "dueDate": _iso(x.due_date), "done": x.done, "doneAt": _iso(x.done_at)}
def _note_view(x: SiteNote) -> dict: return {"id": str(x.id), "siteId": str(x.site_id), "authorName": x.author_name, "text": x.text, "createdAt": _iso(x.created_at)}

def _document_view(db: Session, x: BusinessDocument, lines=None) -> dict:
    if lines is None: lines = db.scalars(select(DocumentLine).where(DocumentLine.document_id == x.id).order_by(DocumentLine.position)).all()
    return {"id": str(x.id), "type": x.document_type, "number": x.number, "status": x.status, "issueDate": _iso(x.issue_date), "dueDate": _iso(x.due_date), "clientId": str(x.client_id) if x.client_id else None, "client": x.client_snapshot, "siteId": str(x.site_id) if x.site_id else None, "subject": x.subject, "notes": x.notes, "paidAt": _iso(x.paid_at), "sourceDocumentId": str(x.source_document_id) if x.source_document_id else None, "version": x.version, "taxRate": float(x.tax_rate or 0), "subtotal": x.subtotal, "taxAmount": x.tax_amount, "total": x.total, "createdAt": _iso(x.created_at), "updatedAt": _iso(x.updated_at), "lines": [{"description": l.description, "quantity": float(l.quantity), "unit": l.unit, "unitPrice": l.unit_price, "amount": l.amount} for l in lines]}

def _chat_view(db: Session, x: Chat) -> dict:
    members = db.scalars(select(ChatMember).where(ChatMember.chat_id == x.id)).all()
    return {"id": str(x.id), "type": x.type, "name": x.name, "memberIds": [str(m.user_id) for m in members], "createdAt": _iso(x.created_at), "announcement": x.announcement, "directUserId": str(x.direct_user_id) if x.direct_user_id else None, "lastMessageText": x.last_message_text, "lastMessageAt": _iso(x.last_message_at), "lastMessageBy": str(x.last_message_by) if x.last_message_by else None, "lastMessageName": x.last_message_name}

def _message_view(db: Session, x: ChatMessage, reactions=None) -> dict:
    if reactions is None:
        reactions = {}
        for r in db.scalars(select(MessageReaction).where(MessageReaction.message_id == x.id)).all(): reactions.setdefault(r.emoji, []).append(str(r.user_id))
    return {"id": str(x.id), "chatId": str(x.chat_id), "senderId": str(x.sender_id) if x.sender_id else None, "senderName": x.sender_name, "senderRole": x.sender_role, "text": x.text, "createdAt": _iso(x.created_at), "replyTo": x.reply_to, "reactions": reactions}

def _read_view(x: ChatRead) -> dict: return {"chatId": str(x.chat_id), "userId": str(x.user_id), "name": x.name, "role": x.role, "seenAt": _iso(x.seen_at)}

def rows(db: Session, domain: str) -> list[dict[str, Any]]:
    if domain == "clients": return [_client_view(x) for x in db.scalars(select(Client)).all()]
    if domain == "sites": return [_site_view(x) for x in db.scalars(select(Site)).all()]
    if domain == "site_members": return [_member_view(x) for x in db.scalars(select(SiteMember)).all()]
    if domain == "site_steps": return [_step_view(x) for x in db.scalars(select(SiteStep)).all()]
    if domain == "site_notes": return [_note_view(x) for x in db.scalars(select(SiteNote)).all()]
    if domain == "documents":
        docs=db.scalars(select(BusinessDocument)).all(); ids=[x.id for x in docs]
        lines_by_doc={}
        if ids:
            for line in db.scalars(select(DocumentLine).where(DocumentLine.document_id.in_(ids)).order_by(DocumentLine.position)).all(): lines_by_doc.setdefault(line.document_id,[]).append(line)
        return [_document_view(db,x,lines_by_doc.get(x.id,[])) for x in docs]
    if domain == "document_versions":
        return [{"id": f"{x.document_id}:{x.version}", "documentId": str(x.document_id), "version": x.version, "snapshot": x.snapshot, "changeLabel": x.change_label, "changedAt": _iso(x.changed_at), "changedByLabel": x.changed_by_label} for x in db.scalars(select(DocumentVersion)).all()]
    if domain == "doc_counters": return [{"id": f"{x.document_type}-{x.year}", "type": x.document_type, "year": x.year, "value": x.value} for x in db.scalars(select(DocumentCounter)).all()]
    if domain == "chats": return [_chat_view(db, x) for x in db.scalars(select(Chat)).all()]
    if domain == "messages":
        messages=db.scalars(select(ChatMessage)).all(); ids=[x.id for x in messages]; reactions_by_message={}
        if ids:
            for reaction in db.scalars(select(MessageReaction).where(MessageReaction.message_id.in_(ids))).all(): reactions_by_message.setdefault(reaction.message_id,{}).setdefault(reaction.emoji,[]).append(str(reaction.user_id))
        return [_message_view(db,x,reactions_by_message.get(x.id,{})) for x in messages]
    if domain == "reads": return [_read_view(x) for x in db.scalars(select(ChatRead)).all()]
    return [dict(r.payload) for r in db.scalars(select(DomainRecord).where(DomainRecord.domain == domain))]

def row(db: Session, domain: str, key: str) -> dict[str, Any] | None:
    if domain in RELATIONAL_DOMAINS:
        if domain == "clients":
            value=db.get(Client,_uuid(key)); return _client_view(value) if value else None
        if domain == "sites":
            value=db.get(Site,_uuid(key)); return _site_view(value) if value else None
        if domain == "site_steps":
            value=db.get(SiteStep,_uuid(key)); return _step_view(value) if value else None
        if domain == "site_notes":
            value=db.get(SiteNote,_uuid(key)); return _note_view(value) if value else None
        if domain == "site_members":
            parts=str(key).split(":",1)
            value=db.get(SiteMember,(_uuid(parts[0]),parts[1])) if len(parts)==2 else None
            return _member_view(value) if value else None
        if domain == "documents":
            value=db.get(BusinessDocument,_uuid(key)); return _document_view(db,value) if value else None
        if domain == "document_versions":
            parts=str(key).rsplit(":",1)
            value=db.get(DocumentVersion,(_uuid(parts[0]),int(parts[1]))) if len(parts)==2 else None
            return {"id":f"{value.document_id}:{value.version}","documentId":str(value.document_id),"version":value.version,"snapshot":value.snapshot,"changeLabel":value.change_label,"changedAt":_iso(value.changed_at),"changedByLabel":value.changed_by_label} if value else None
        if domain == "doc_counters":
            parts=str(key).rsplit("-",1)
            value=db.get(DocumentCounter,(parts[0],int(parts[1]))) if len(parts)==2 else None
            return {"id":f"{value.document_type}-{value.year}","type":value.document_type,"year":value.year,"value":value.value} if value else None
        if domain == "chats":
            value=db.get(Chat,_uuid(key)); return _chat_view(db,value) if value else None
        if domain == "messages":
            value=db.get(ChatMessage,_uuid(key)); return _message_view(db,value) if value else None
        if domain == "reads":
            parts=str(key).split(":",1)
            value=db.get(ChatRead,(_uuid(parts[0]),_uuid(parts[1]))) if len(parts)==2 else None
            return _read_view(value) if value else None
    rec = db.get(DomainRecord, f"{domain}:{key}")
    return dict(rec.payload) if rec else None

def save(db: Session, domain: str, payload: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    data = dict(payload); key = key or str(data.get("id") or uuid4()); data["id"] = key
    if domain == "clients":
        x = db.get(Client, _uuid(key))
        if x is None: x = Client(id=_uuid(key), name="")
        db.add(x)
        for source, target in (("name","name"),("contactName","contact_name"),("phone","phone"),("email","email"),("address","address"),("nif","nif"),("stat","stat"),("notes","notes")):
            if source in data: setattr(x, target, data[source])
    elif domain == "sites":
        x = db.get(Site, _uuid(key))
        if x is None: x = Site(id=_uuid(key), name="")
        db.add(x)
        for source, target in (("name","name"),("clientId","client_id"),("workplaceId","workplace_id"),("address","address"),("description","description"),("status","status"),("manualProgress","manual_progress")):
            if source in data: setattr(x, target, _uuid(data[source]) if target.endswith("_id") and data[source] else (data[source] if target not in ("client_id","workplace_id") else None))
        if "clientSnapshot" in data: x.client_snapshot=data["clientSnapshot"]
        if "startDate" in data: x.start_date=_day(data["startDate"])
        if "endDate" in data: x.end_date=_day(data["endDate"])
        if "createdAt" in data: x.created_at=_dt(data["createdAt"], x.created_at)
    elif domain == "site_members":
        site_id=_uuid(data.get("siteId")); code=data.get("employeeCode"); x=db.get(SiteMember,(site_id,code)) or SiteMember(site_id=site_id,employee_code=code); db.add(x)
    elif domain == "site_steps":
        x=db.get(SiteStep,_uuid(key))
        if x is None:x=SiteStep(id=_uuid(key),site_id=_uuid(data.get("siteId")),title="")
        db.add(x)
        for source,target in (("siteId","site_id"),("title","title"),("description","description"),("position","position"),("done","done")): 
            if source in data: setattr(x,target,_uuid(data[source]) if target=="site_id" else data[source])
        if "dueDate" in data:x.due_date=_day(data["dueDate"])
        if "doneAt" in data:x.done_at=_dt(data["doneAt"])
    elif domain == "site_notes":
        x=db.get(SiteNote,_uuid(key))
        if x is None:x=SiteNote(id=_uuid(key),site_id=_uuid(data.get("siteId")),author_name="",text="")
        db.add(x)
        for source,target in (("siteId","site_id"),("authorName","author_name"),("text","text")): 
            if source in data:setattr(x,target,_uuid(data[source]) if target=="site_id" else data[source])
        if "createdAt" in data:x.created_at=_dt(data["createdAt"],x.created_at)
    elif domain == "documents":
        x=db.get(BusinessDocument,_uuid(key))
        if x is None:x=BusinessDocument(id=_uuid(key),document_type=data.get("type","DEVIS"),number=data.get("number",key),status=data.get("status","BROUILLON"),issue_date=_day(data.get("issueDate")) or date.today())
        db.add(x)
        for source,target in (("type","document_type"),("number","number"),("status","status"),("subject","subject"),("notes","notes"),("version","version"),("subtotal","subtotal"),("taxAmount","tax_amount"),("total","total"),("taxRate","tax_rate")): 
            if source in data:setattr(x,target,data[source])
        for source,target in (("clientId","client_id"),("siteId","site_id"),("sourceDocumentId","source_document_id")):
            if source in data:setattr(x,target,_uuid(data[source]) if data[source] else None)
        if "client" in data:x.client_snapshot=data["client"] or {}
        if "issueDate" in data:x.issue_date=_day(data["issueDate"]) or x.issue_date
        if "dueDate" in data:x.due_date=_day(data["dueDate"])
        if "paidAt" in data:x.paid_at=_day(data["paidAt"])
        if "createdAt" in data:x.created_at=_dt(data["createdAt"],x.created_at)
        if "updatedAt" in data:x.updated_at=_dt(data["updatedAt"],x.updated_at) or x.updated_at
        if "lines" in data:
            db.execute(delete(DocumentLine).where(DocumentLine.document_id==x.id))
            for i,line in enumerate(data["lines"]): db.add(DocumentLine(id=uuid4(),document_id=x.id,position=i,description=line.get("description", ""),quantity=line.get("quantity",0),unit=line.get("unit", ""),unit_price=line.get("unitPrice",0),amount=line.get("amount",0)))
    elif domain == "document_versions":
        x=db.get(DocumentVersion,(_uuid(data.get("documentId")),int(data.get("version",1)))) or DocumentVersion(document_id=_uuid(data.get("documentId")),version=int(data.get("version",1)),snapshot=data.get("snapshot",{}),change_label=data.get("changeLabel", ""),changed_at=_dt(data.get("changedAt"),datetime.now(timezone.utc)),changed_by_label=data.get("changedByLabel", "")); db.add(x)
    elif domain == "doc_counters":
        x=db.get(DocumentCounter,(data.get("type"),int(data.get("year")))) or DocumentCounter(document_type=data.get("type"),year=int(data.get("year")),value=0); db.add(x); x.value=int(data.get("value",0))
    elif domain == "chats":
        x=db.get(Chat,_uuid(key))
        if x is None:x=Chat(id=_uuid(key),type=data.get("type","group"))
        db.add(x)
        for source,target in (("type","type"),("name","name"),("announcement","announcement")): 
            if source in data:setattr(x,target,data[source])
        if "directUserId" in data:x.direct_user_id=_uuid(data["directUserId"])
        if "createdAt" in data:x.created_at=_dt(data["createdAt"],x.created_at)
        if "memberIds" in data:
            db.execute(delete(ChatMember).where(ChatMember.chat_id==x.id))
            for uid in data["memberIds"]: db.add(ChatMember(chat_id=x.id,user_id=_uuid(uid)))
        for source,target in (("lastMessageText","last_message_text"),("lastMessageBy","last_message_by"),("lastMessageName","last_message_name")): 
            if source in data:setattr(x,target,_uuid(data[source]) if target=="last_message_by" and data[source] else data[source])
        if "lastMessageAt" in data:x.last_message_at=_dt(data["lastMessageAt"])
    elif domain == "messages":
        x=db.get(ChatMessage,_uuid(key))
        if x is None:x=ChatMessage(id=_uuid(key),chat_id=_uuid(data.get("chatId")),sender_name="",sender_role="",text="")
        db.add(x)
        for source,target in (("chatId","chat_id"),("senderId","sender_id"),("senderName","sender_name"),("senderRole","sender_role"),("text","text"),("replyTo","reply_to")): 
            if source in data:setattr(x,target,_uuid(data[source]) if target in ("chat_id","sender_id") and data[source] else data[source])
        if "createdAt" in data:x.created_at=_dt(data["createdAt"],x.created_at)
        db.execute(delete(MessageReaction).where(MessageReaction.message_id==x.id))
        for emoji, users in (data.get("reactions") or {}).items():
            for uid in users: db.add(MessageReaction(message_id=x.id,user_id=_uuid(uid),emoji=emoji))
    elif domain == "reads":
        x=db.get(ChatRead,(_uuid(data.get("chatId")),_uuid(data.get("userId")))) or ChatRead(chat_id=_uuid(data.get("chatId")),user_id=_uuid(data.get("userId")),name="",role="",seen_at=datetime.now(timezone.utc)); db.add(x)
        x.name=data.get("name","");x.role=data.get("role","");x.seen_at=_dt(data.get("seenAt"),x.seen_at)
    else:
        rec=db.get(DomainRecord,f"{domain}:{key}")
        if rec is None:db.add(DomainRecord(id=f"{domain}:{key}",domain=domain,payload=data))
        else:rec.payload=data
    db.flush(); return data

def remove(db: Session, domain: str, key: str) -> None:
    if domain == "clients": db.delete(db.get(Client,_uuid(key)))
    elif domain == "sites": db.delete(db.get(Site,_uuid(key)))
    elif domain == "site_members":
        parts=key.split(":",1); db.delete(db.get(SiteMember,(_uuid(parts[0]),parts[1])))
    elif domain == "site_steps": db.delete(db.get(SiteStep,_uuid(key)))
    elif domain == "site_notes": db.delete(db.get(SiteNote,_uuid(key)))
    elif domain == "documents": db.delete(db.get(BusinessDocument,_uuid(key)))
    elif domain == "document_versions":
        parts=key.rsplit(":",1); db.delete(db.get(DocumentVersion,(_uuid(parts[0]),int(parts[1]))))
    elif domain == "chats": db.delete(db.get(Chat,_uuid(key)))
    elif domain == "messages": db.delete(db.get(ChatMessage,_uuid(key)))
    elif domain == "reads":
        parts=key.split(":",1); db.delete(db.get(ChatRead,(_uuid(parts[0]),_uuid(parts[1]))))
    elif domain == "doc_counters":
        parts=key.rsplit("-",1); db.delete(db.get(DocumentCounter,(parts[0],int(parts[1]))))
    else: db.execute(delete(DomainRecord).where(DomainRecord.id==f"{domain}:{key}"))

def lock_domain(db: Session, domain: str, key: str = "all") -> None:
    from sqlalchemy import text
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {"lock_key": f"fortico:{domain}:{key}"})
def new_id() -> str: return str(uuid4())
def now_utc() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or now_utc()).isoformat(timespec="milliseconds").replace("+00:00", "Z")

class SocketHub:
    def __init__(self): self.connections: dict[str, tuple[WebSocket, str, str, str | None]] = {}
    async def connect(self, ws: WebSocket, uid: str, role: str, employee_code: str | None): await ws.accept(); self.connections[uid] = (ws, role, uid, employee_code)
    def disconnect(self, uid: str): self.connections.pop(uid, None)
    async def publish(self, event: dict[str, Any], recipients: set[str] | None = None):
        dead=[]
        for uid,(ws,role,_,code) in list(self.connections.items()):
            if recipients is not None and uid not in recipients: continue
            try: await ws.send_json(event)
            except Exception: dead.append(uid)
        for uid in dead: self.disconnect(uid)
    async def admins(self, db: Session) -> set[str]: return {str(u.id) for u in db.scalars(select(User).where(User.role == "admin", User.active.is_(True)))}
hub = SocketHub()
def _queue_event(value):
    try:
        from .main import event_queue; event_queue.append(value)
    except Exception: pass
@event.listens_for(Session, "after_commit")
def _publish_committed(session):
    for value in session.info.pop("fortico_events_after_commit", []): _queue_event(value)
@event.listens_for(Session, "after_rollback")
def _discard_rolled_back(session): session.info.pop("fortico_events_after_commit", None)
def emit_after_commit(db: Session, event: dict[str, Any], recipients: set[str] | None = None):
    value=(event,recipients)
    if db.in_transaction(): db.info.setdefault("fortico_events_after_commit", []).append(value)
    else: _queue_event(value)
