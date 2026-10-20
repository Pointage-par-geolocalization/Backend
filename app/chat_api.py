from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .core import admin, current_user, employee, fail, get_db, user_payload
from .models import Chat, ChatMember, ChatMessage, ChatRead, Employee, MessageReaction, Role, User
from .store import emit_after_commit, iso, lock_domain, new_id, now_utc, row, rows, save


router = APIRouter(tags=["chat"])

QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
MAX_MESSAGE_LENGTH = 2000
MAX_REPLY_LENGTH = 140
MAX_GROUP_NAME_LENGTH = 60


class GroupIn(BaseModel):
    name: str
    announcement: bool = False


class GroupMemberIn(BaseModel):
    employeeCode: str | None = None
    employeeCodes: list[str] = Field(default_factory=list, max_length=100)
class DirectIn(BaseModel):
    employeeCode: str


class MessageIn(BaseModel):
    text: str
    replyToId: str | None = None


def _uid(user: User) -> str:
    return str(user.id)


def _role(user: User) -> str:
    return user.role.value if hasattr(user.role, "value") else str(user.role)


def _identity(db: Session, user: User) -> tuple[str, str]:
    """Return the display identity from the authenticated user, never from request data."""
    payload = user_payload(user, db)
    return _uid(user), payload.get("name") or "Administrateur"


def _chat(db: Session, chat_id: str) -> dict[str, Any]:
    value = row(db, "chats", chat_id)
    if value is None:
        fail(404, "NOT_FOUND", "Conversation introuvable.")
    return value


def _can_see(chat: dict[str, Any], user: User) -> bool:
    if _role(user) == "admin":
        return True
    if chat.get("type") == "group":
        return _uid(user) in (chat.get("memberIds") or [])
    return chat.get("directUserId") == _uid(user)


def _can_read(chat: dict[str, Any], user: User) -> bool:
    return _role(user) == "admin" or _uid(user) in (chat.get("memberIds") or [])


def _readable_chat(db: Session, chat_id: str, user: User) -> dict[str, Any]:
    chat = _chat(db, chat_id)
    if not _can_read(chat, user):
        fail(403, "FORBIDDEN", "Vous ne faites pas partie de cette conversation.")
    return chat


def _active_admin_ids(db: Session) -> set[str]:
    return {
        str(value.id)
        for value in db.scalars(select(User).where(User.role == Role.admin, User.active.is_(True)))
    }


def _recipients(db: Session, chat: dict[str, Any]) -> set[str]:
    # Les groupes et les directs sont limités aux administrateurs et aux membres concernés.
    recipients = _active_admin_ids(db) | {str(value) for value in (chat.get("memberIds") or [])}
    return recipients


def _emit(
    db: Session,
    event_type: str,
    chat: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
    additional_recipients: set[str] | None = None,
) -> None:
    event: dict[str, Any] = {"type": event_type, "chatId": str(chat["id"])}
    if extra:
        event.update(extra)
    recipients = _recipients(db, chat)
    if additional_recipients:
        recipients.update(additional_recipients)
    try:
        emit_after_commit(db, event, recipients)
    except ImportError:
        # Older app.main versions do not declare the queue yet; keep the
        # store contract intact and create the queue lazily for this process.
        from . import main

        if not hasattr(main, "event_queue"):
            main.event_queue = []
        emit_after_commit(db, event, recipients)


def _message_rows(db: Session, chat_id: str) -> list[dict[str, Any]]:
    return [value for value in rows(db, "messages") if value.get("chatId") == chat_id]


def _read_row(db: Session, chat_id: str, user_id: str) -> dict[str, Any] | None:
    return row(db, "reads", f"{chat_id}:{user_id}")


def _mark_read(db: Session, chat: dict[str, Any], user: User) -> None:
    user_id, name = _identity(db, user)
    save(
        db,
        "reads",
        {"chatId": chat["id"], "userId": user_id, "name": name, "role": _role(user), "seenAt": iso(now_utc())},
        key=f"{chat['id']}:{user_id}",
    )


def _preview_from_messages(db: Session, chat: dict[str, Any]) -> None:
    messages = _message_rows(db, str(chat["id"]))
    latest = max(messages, key=lambda value: value.get("createdAt", ""), default=None)
    if latest is None:
        chat.update(lastMessageText=None, lastMessageAt=None, lastMessageBy=None, lastMessageName=None)
    else:
        chat.update(
            lastMessageText=str(latest.get("text", ""))[:120],
            lastMessageAt=latest.get("createdAt"),
            lastMessageBy=latest.get("senderId"),
            lastMessageName=latest.get("senderName"),
        )
    save(db, "chats", chat, key=str(chat["id"]))


def _chat_view(db: Session, chat: dict[str, Any], user: User) -> dict[str, Any]:
    user_id = _uid(user)
    seen = (_read_row(db, str(chat["id"]), user_id) or {}).get("seenAt", "")
    unread = 0
    if _can_read(chat, user):
        unread = sum(
            1
            for message in _message_rows(db, str(chat["id"]))
            if message.get("senderId") != user_id and message.get("createdAt", "") > seen
        )

    direct_name: str | None = None
    direct_user_id = chat.get("directUserId")
    if direct_user_id:
        direct_user = db.get(User, direct_user_id)
        if direct_user and direct_user.employee_code:
            direct_employee = db.get(Employee, direct_user.employee_code)
            if direct_employee:
                direct_name = f"{direct_employee.first_name} {direct_employee.last_name}"

    return {
        "id": str(chat["id"]),
        "type": chat.get("type"),
        "name": chat.get("name", ""),
        "memberIds": [str(value) for value in (chat.get("memberIds") or [])],
        "createdAt": chat.get("createdAt"),
        "announcement": bool(chat.get("announcement", False)),
        "directUserId": direct_user_id,
        "directEmployeeName": direct_name,
        "lastMessageText": chat.get("lastMessageText"),
        "lastMessageAt": chat.get("lastMessageAt"),
        "lastMessageBy": chat.get("lastMessageBy"),
        "lastMessageName": chat.get("lastMessageName"),
        "unreadCount": unread,
    }


def _clean_required(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _message_view(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(message["id"]),
        "senderId": message.get("senderId"),
        "senderName": message.get("senderName", ""),
        "senderRole": message.get("senderRole"),
        "text": message.get("text", ""),
        "createdAt": message.get("createdAt"),
        "replyTo": message.get("replyTo"),
        "reactions": message.get("reactions") or {},
    }


def _read_view(read: dict[str, Any]) -> dict[str, Any]:
    return {
        "userId": str(read.get("userId")),
        "name": read.get("name", ""),
        "role": read.get("role"),
        "seenAt": read.get("seenAt"),
    }


@router.get("/chats")
def list_chats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_chat_view(db, chat, user) for chat in rows(db, "chats") if _can_see(chat, user)]


@router.post("/chats/groups")
def create_group(data: GroupIn, user: User = Depends(admin), db: Session = Depends(get_db)):
    name = _clean_required(data.name)
    if not name:
        fail(400, "VALIDATION_ERROR", "Le nom du groupe est obligatoire.")
    lock_domain(db, "chats")
    chat = {
        "id": new_id(),
        "type": "group",
        "name": name[:MAX_GROUP_NAME_LENGTH],
        "announcement": data.announcement is True,
        "memberIds": [],
        "createdAt": iso(now_utc()),
        "directUserId": None,
        "directEmployeeName": None,
        "lastMessageText": None,
        "lastMessageAt": None,
        "lastMessageBy": None,
        "lastMessageName": None,
    }
    save(db, "chats", chat, key=chat["id"])
    db.commit()
    _emit(db, "chat.updated", chat)
    return {"id": chat["id"]}


@router.post("/chats/direct")
def open_direct(data: DirectIn, user: User = Depends(admin), db: Session = Depends(get_db)):
    code = _clean_required(data.employeeCode).upper()
    if not code:
        fail(404, "NOT_FOUND", "Salarié introuvable.")
    lock_domain(db, "chats", f"direct:{code}")
    target_employee = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code))
    if not target_employee:
        fail(404, "NOT_FOUND", "Salarié introuvable.")
    target_user = db.scalar(select(User).where(User.employee_code == target_employee.employee_code))
    if not target_user:
        fail(404, "NOT_FOUND", "Ce salarié n'a pas de compte de connexion.")
    target_id = str(target_user.id)
    existing = next(
        (chat for chat in rows(db, "chats") if chat.get("type") == "direct" and chat.get("directUserId") == target_id),
        None,
    )
    if existing:
        return {"id": existing["id"]}

    chat = {
        "id": new_id(),
        "type": "direct",
        "name": "",
        "announcement": False,
        "memberIds": [target_id],
        "createdAt": iso(now_utc()),
        "directUserId": target_id,
        "directEmployeeName": f"{target_employee.first_name} {target_employee.last_name}",
        "lastMessageText": None,
        "lastMessageAt": None,
        "lastMessageBy": None,
        "lastMessageName": None,
    }
    save(db, "chats", chat, key=chat["id"])
    db.commit()
    _emit(db, "chat.updated", chat)
    return {"id": chat["id"]}


@router.post("/chats/{chat_id}/members", status_code=204)
def add_group_member(chat_id: str, data: GroupMemberIn, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db, "chats", chat_id)
    chat = _chat(db, chat_id)
    if chat.get("type") != "group": fail(404, "NOT_FOUND", "Groupe introuvable.")
    submitted_codes = data.employeeCodes or ([data.employeeCode] if data.employeeCode else [])
    codes = list(dict.fromkeys(code.strip() for code in submitted_codes if code and code.strip()))
    if not codes: fail(400, "VALIDATION_ERROR", "Sélectionnez au moins un salarié actif.")
    members = [str(v) for v in (chat.get("memberIds") or [])]
    changed = False
    for code in codes:
        target = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code.upper()))
        if not target or not target.active: fail(404, "NOT_FOUND", f"Salarié actif introuvable : {code}.")
        account = db.scalar(select(User).where(User.employee_code == target.employee_code, User.active.is_(True)))
        if not account: fail(404, "NOT_FOUND", f"Compte actif introuvable pour le salarié {code}.")
        if str(account.id) not in members:
            members.append(str(account.id))
            changed = True
    if changed:
        chat["memberIds"] = members
        save(db, "chats", chat, key=chat_id)
        _emit(db, "chat.updated", chat)
    db.commit()


@router.get("/chats/{chat_id}/members")
def list_group_members(chat_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    chat = _chat(db, chat_id)
    if chat.get("type") != "group": fail(404, "NOT_FOUND", "Groupe introuvable.")
    members = []
    for user_id in chat.get("memberIds") or []:
        account = db.get(User, UUID(str(user_id)))
        if not account or not account.employee_code:
            continue
        employee_record = db.get(Employee, account.employee_code)
        if employee_record:
            members.append({
                "employeeCode": employee_record.employee_code,
                "firstName": employee_record.first_name,
                "lastName": employee_record.last_name,
                "position": employee_record.position,
                "active": employee_record.active,
            })
    return sorted(members, key=lambda member: (member["lastName"].casefold(), member["firstName"].casefold()))


@router.delete("/chats/{chat_id}/members/{employee_code}", status_code=204)
def remove_group_member(
    chat_id: str,
    employee_code: str,
    user: User = Depends(admin),
    db: Session = Depends(get_db),
):
    lock_domain(db, "chats", chat_id)
    chat = _chat(db, chat_id)
    if chat.get("type") != "group": fail(404, "NOT_FOUND", "Groupe introuvable.")
    code = employee_code.strip()
    target = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code.upper()))
    if not target:
        fail(404, "NOT_FOUND", "Salarié introuvable.")
    account = db.scalar(select(User).where(User.employee_code == target.employee_code))
    if not account or str(account.id) not in [str(member_id) for member_id in (chat.get("memberIds") or [])]:
        fail(404, "NOT_FOUND", "Ce salarié n’est pas membre de ce groupe.")
    chat["memberIds"] = [str(member_id) for member_id in (chat.get("memberIds") or []) if str(member_id) != str(account.id)]
    save(db, "chats", chat, key=chat_id)
    _emit(db, "chat.updated", chat, additional_recipients={str(account.id)})
    db.commit()


@router.post("/chats/{chat_id}/join", status_code=204)
def join_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
    fail(403, "ADMIN_REQUIRED", "Seul un administrateur peut ajouter un salarié à un groupe.")


@router.post("/chats/{chat_id}/leave", status_code=204)
def leave_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
    lock_domain(db, "chats", chat_id)
    chat = _chat(db, chat_id)
    if chat.get("type") != "group":
        fail(404, "NOT_FOUND", "Groupe introuvable.")
    chat["memberIds"] = [str(value) for value in (chat.get("memberIds") or []) if str(value) != _uid(user)]
    save(db, "chats", chat, key=chat_id)
    db.commit()
    _emit(db, "chat.updated", chat)


@router.get("/chats/{chat_id}/messages")
def get_messages(
    chat_id: str,
    limit: int = Query(default=300, ge=1),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    chat = _readable_chat(db, chat_id, user)
    del chat
    messages = sorted(_message_rows(db, chat_id), key=lambda value: value.get("createdAt", ""))[-limit:]
    return [_message_view(message) for message in messages]


@router.post("/chats/{chat_id}/messages", status_code=204)
def send_message(chat_id: str, data: MessageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    lock_domain(db, "chats", chat_id)
    chat = _readable_chat(db, chat_id, user)
    if chat.get("announcement") and _role(user) != "admin":
        fail(403, "ANNOUNCEMENT_READ_ONLY", "Groupe d'annonces : seul l'administrateur peut écrire.")
    text = _clean_required(data.text)
    if not text:
        fail(400, "VALIDATION_ERROR", "Le message est vide.")
    if len(text) > MAX_MESSAGE_LENGTH:
        fail(400, "VALIDATION_ERROR", f"Message trop long ({MAX_MESSAGE_LENGTH} caractères maximum).")

    quoted = None
    if data.replyToId:
        candidate = row(db, "messages", data.replyToId)
        if candidate and candidate.get("chatId") == chat_id:
            quoted = {
                "id": str(candidate["id"]),
                "senderName": candidate.get("senderName", ""),
                "text": str(candidate.get("text", ""))[:MAX_REPLY_LENGTH],
            }
    sender_id, sender_name = _identity(db, user)
    message = {
        "id": new_id(),
        "chatId": chat_id,
        "senderId": sender_id,
        "senderName": sender_name,
        "senderRole": _role(user),
        "text": text,
        "createdAt": iso(now_utc()),
        "replyTo": quoted,
        "reactions": {},
    }
    save(db, "messages", message, key=message["id"])
    chat.update(
        lastMessageText=text[:120],
        lastMessageAt=message["createdAt"],
        lastMessageBy=sender_id,
        lastMessageName=sender_name,
    )
    save(db, "chats", chat, key=chat_id)
    _mark_read(db, chat, user)
    db.commit()
    _emit(db, "message.created", chat, extra={"messageId": message["id"]})


@router.put("/chats/{chat_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
def toggle_reaction(
    chat_id: str,
    message_id: str,
    emoji: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    lock_domain(db, "chats", chat_id)
    chat = _readable_chat(db, chat_id, user)
    if emoji not in QUICK_REACTIONS:
        fail(400, "VALIDATION_ERROR", "Réaction non autorisée.")
    message = row(db, "messages", message_id)
    if not message or message.get("chatId") != chat_id:
        fail(404, "NOT_FOUND", "Message introuvable.")
    reactions = dict(message.get("reactions") or {})
    reacting_users = [str(value) for value in (reactions.get(emoji) or [])]
    user_id = _uid(user)
    if user_id in reacting_users:
        reacting_users = [value for value in reacting_users if value != user_id]
    else:
        reacting_users.append(user_id)
    if reacting_users:
        reactions[emoji] = reacting_users
    else:
        reactions.pop(emoji, None)
    message["reactions"] = reactions
    save(db, "messages", message, key=message_id)
    db.commit()
    _emit(db, "message.reactions", chat, extra={"messageId": message_id})


@router.delete("/chats/{chat_id}", status_code=204)
def delete_chat(chat_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db, "chats", chat_id)
    chat = _chat(db, chat_id)
    chat_pk = UUID(chat_id)
    message_ids = select(ChatMessage.id).where(ChatMessage.chat_id == chat_pk)
    db.execute(delete(MessageReaction).where(MessageReaction.message_id.in_(message_ids)))
    db.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_pk))
    db.execute(delete(ChatRead).where(ChatRead.chat_id == chat_pk))
    db.execute(delete(ChatMember).where(ChatMember.chat_id == chat_pk))
    chat_record = db.get(Chat, chat_pk)
    if chat_record is not None:
        db.delete(chat_record)
    _emit(db, "chat.deleted", chat)
    db.commit()


@router.put("/chats/{chat_id}/read", status_code=204)
def mark_seen(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    chat = _readable_chat(db, chat_id, user)
    lock_domain(db, "reads", chat_id)
    _mark_read(db, chat, user)
    db.commit()
    _emit(db, "chat.read", chat, extra={"userId": _uid(user)})


@router.get("/chats/{chat_id}/reads")
def get_reads(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _readable_chat(db, chat_id, user)
    return [_read_view(read) for read in rows(db, "reads") if read.get("chatId") == chat_id]



# from __future__ import annotations

# from typing import Any
# from uuid import UUID

# from fastapi import APIRouter, Depends, Query
# from pydantic import BaseModel, Field
# from sqlalchemy import delete, func, select
# from sqlalchemy.orm import Session

# from .core import admin, current_user, employee, fail, get_db, user_payload
# from .models import Chat, ChatMember, ChatMessage, ChatRead, Employee, MessageReaction, Role, User
# from .store import emit_after_commit, iso, lock_domain, new_id, now_utc, row, rows, save


# router = APIRouter(tags=["chat"])

# QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
# MAX_MESSAGE_LENGTH = 2000
# MAX_REPLY_LENGTH = 140
# MAX_GROUP_NAME_LENGTH = 60


# class GroupIn(BaseModel):
#     name: str
#     announcement: bool = False


# class GroupMemberIn(BaseModel):
#     employeeCode: str | None = None
#     employeeCodes: list[str] = Field(default_factory=list, max_length=100)
# class DirectIn(BaseModel):
#     employeeCode: str


# class MessageIn(BaseModel):
#     text: str
#     replyToId: str | None = None


# def _uid(user: User) -> str:
#     return str(user.id)


# def _role(user: User) -> str:
#     return user.role.value if hasattr(user.role, "value") else str(user.role)


# def _identity(db: Session, user: User) -> tuple[str, str]:
#     """Return the display identity from the authenticated user, never from request data."""
#     payload = user_payload(user, db)
#     return _uid(user), payload.get("name") or "Administrateur"


# def _chat(db: Session, chat_id: str) -> dict[str, Any]:
#     value = row(db, "chats", chat_id)
#     if value is None:
#         fail(404, "NOT_FOUND", "Conversation introuvable.")
#     return value


# def _can_see(chat: dict[str, Any], user: User) -> bool:
#     if _role(user) == "admin":
#         return True
#     if chat.get("type") == "group":
#         return _uid(user) in (chat.get("memberIds") or [])
#     return chat.get("directUserId") == _uid(user)


# def _can_read(chat: dict[str, Any], user: User) -> bool:
#     return _role(user) == "admin" or _uid(user) in (chat.get("memberIds") or [])


# def _readable_chat(db: Session, chat_id: str, user: User) -> dict[str, Any]:
#     chat = _chat(db, chat_id)
#     if not _can_read(chat, user):
#         fail(403, "FORBIDDEN", "Vous ne faites pas partie de cette conversation.")
#     return chat


# def _active_admin_ids(db: Session) -> set[str]:
#     return {
#         str(value.id)
#         for value in db.scalars(select(User).where(User.role == Role.admin, User.active.is_(True)))
#     }


# def _recipients(db: Session, chat: dict[str, Any]) -> set[str]:
#     # Les groupes et les directs sont limités aux administrateurs et aux membres concernés.
#     recipients = _active_admin_ids(db) | {str(value) for value in (chat.get("memberIds") or [])}
#     return recipients


# def _emit(db: Session, event_type: str, chat: dict[str, Any], *, extra: dict[str, Any] | None = None) -> None:
#     event: dict[str, Any] = {"type": event_type, "chatId": str(chat["id"])}
#     if extra:
#         event.update(extra)
#     recipients = _recipients(db, chat)
#     try:
#         emit_after_commit(db, event, recipients)
#     except ImportError:
#         # Older app.main versions do not declare the queue yet; keep the
#         # store contract intact and create the queue lazily for this process.
#         from . import main

#         if not hasattr(main, "event_queue"):
#             main.event_queue = []
#         emit_after_commit(db, event, recipients)


# def _message_rows(db: Session, chat_id: str) -> list[dict[str, Any]]:
#     return [value for value in rows(db, "messages") if value.get("chatId") == chat_id]


# def _read_row(db: Session, chat_id: str, user_id: str) -> dict[str, Any] | None:
#     return row(db, "reads", f"{chat_id}:{user_id}")


# def _mark_read(db: Session, chat: dict[str, Any], user: User) -> None:
#     user_id, name = _identity(db, user)
#     save(
#         db,
#         "reads",
#         {"chatId": chat["id"], "userId": user_id, "name": name, "role": _role(user), "seenAt": iso(now_utc())},
#         key=f"{chat['id']}:{user_id}",
#     )


# def _preview_from_messages(db: Session, chat: dict[str, Any]) -> None:
#     messages = _message_rows(db, str(chat["id"]))
#     latest = max(messages, key=lambda value: value.get("createdAt", ""), default=None)
#     if latest is None:
#         chat.update(lastMessageText=None, lastMessageAt=None, lastMessageBy=None, lastMessageName=None)
#     else:
#         chat.update(
#             lastMessageText=str(latest.get("text", ""))[:120],
#             lastMessageAt=latest.get("createdAt"),
#             lastMessageBy=latest.get("senderId"),
#             lastMessageName=latest.get("senderName"),
#         )
#     save(db, "chats", chat, key=str(chat["id"]))


# def _chat_view(db: Session, chat: dict[str, Any], user: User) -> dict[str, Any]:
#     user_id = _uid(user)
#     seen = (_read_row(db, str(chat["id"]), user_id) or {}).get("seenAt", "")
#     unread = 0
#     if _can_read(chat, user):
#         unread = sum(
#             1
#             for message in _message_rows(db, str(chat["id"]))
#             if message.get("senderId") != user_id and message.get("createdAt", "") > seen
#         )

#     direct_name: str | None = None
#     direct_user_id = chat.get("directUserId")
#     if direct_user_id:
#         direct_user = db.get(User, direct_user_id)
#         if direct_user and direct_user.employee_code:
#             direct_employee = db.get(Employee, direct_user.employee_code)
#             if direct_employee:
#                 direct_name = f"{direct_employee.first_name} {direct_employee.last_name}"

#     return {
#         "id": str(chat["id"]),
#         "type": chat.get("type"),
#         "name": chat.get("name", ""),
#         "memberIds": [str(value) for value in (chat.get("memberIds") or [])],
#         "createdAt": chat.get("createdAt"),
#         "announcement": bool(chat.get("announcement", False)),
#         "directUserId": direct_user_id,
#         "directEmployeeName": direct_name,
#         "lastMessageText": chat.get("lastMessageText"),
#         "lastMessageAt": chat.get("lastMessageAt"),
#         "lastMessageBy": chat.get("lastMessageBy"),
#         "lastMessageName": chat.get("lastMessageName"),
#         "unreadCount": unread,
#     }


# def _clean_required(value: Any) -> str:
#     return value.strip() if isinstance(value, str) else ""


# def _message_view(message: dict[str, Any]) -> dict[str, Any]:
#     return {
#         "id": str(message["id"]),
#         "senderId": message.get("senderId"),
#         "senderName": message.get("senderName", ""),
#         "senderRole": message.get("senderRole"),
#         "text": message.get("text", ""),
#         "createdAt": message.get("createdAt"),
#         "replyTo": message.get("replyTo"),
#         "reactions": message.get("reactions") or {},
#     }


# def _read_view(read: dict[str, Any]) -> dict[str, Any]:
#     return {
#         "userId": str(read.get("userId")),
#         "name": read.get("name", ""),
#         "role": read.get("role"),
#         "seenAt": read.get("seenAt"),
#     }


# @router.get("/chats")
# def list_chats(user: User = Depends(current_user), db: Session = Depends(get_db)):
#     return [_chat_view(db, chat, user) for chat in rows(db, "chats") if _can_see(chat, user)]


# @router.post("/chats/groups")
# def create_group(data: GroupIn, user: User = Depends(admin), db: Session = Depends(get_db)):
#     name = _clean_required(data.name)
#     if not name:
#         fail(400, "VALIDATION_ERROR", "Le nom du groupe est obligatoire.")
#     lock_domain(db, "chats")
#     chat = {
#         "id": new_id(),
#         "type": "group",
#         "name": name[:MAX_GROUP_NAME_LENGTH],
#         "announcement": data.announcement is True,
#         "memberIds": [],
#         "createdAt": iso(now_utc()),
#         "directUserId": None,
#         "directEmployeeName": None,
#         "lastMessageText": None,
#         "lastMessageAt": None,
#         "lastMessageBy": None,
#         "lastMessageName": None,
#     }
#     save(db, "chats", chat, key=chat["id"])
#     db.commit()
#     _emit(db, "chat.updated", chat)
#     return {"id": chat["id"]}


# @router.post("/chats/direct")
# def open_direct(data: DirectIn, user: User = Depends(admin), db: Session = Depends(get_db)):
#     code = _clean_required(data.employeeCode).upper()
#     if not code:
#         fail(404, "NOT_FOUND", "Salarié introuvable.")
#     lock_domain(db, "chats", f"direct:{code}")
#     target_employee = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code))
#     if not target_employee:
#         fail(404, "NOT_FOUND", "Salarié introuvable.")
#     target_user = db.scalar(select(User).where(User.employee_code == target_employee.employee_code))
#     if not target_user:
#         fail(404, "NOT_FOUND", "Ce salarié n'a pas de compte de connexion.")
#     target_id = str(target_user.id)
#     existing = next(
#         (chat for chat in rows(db, "chats") if chat.get("type") == "direct" and chat.get("directUserId") == target_id),
#         None,
#     )
#     if existing:
#         return {"id": existing["id"]}

#     chat = {
#         "id": new_id(),
#         "type": "direct",
#         "name": "",
#         "announcement": False,
#         "memberIds": [target_id],
#         "createdAt": iso(now_utc()),
#         "directUserId": target_id,
#         "directEmployeeName": f"{target_employee.first_name} {target_employee.last_name}",
#         "lastMessageText": None,
#         "lastMessageAt": None,
#         "lastMessageBy": None,
#         "lastMessageName": None,
#     }
#     save(db, "chats", chat, key=chat["id"])
#     db.commit()
#     _emit(db, "chat.updated", chat)
#     return {"id": chat["id"]}


# @router.post("/chats/{chat_id}/members", status_code=204)
# def add_group_member(chat_id: str, data: GroupMemberIn, user: User = Depends(admin), db: Session = Depends(get_db)):
#     lock_domain(db, "chats", chat_id)
#     chat = _chat(db, chat_id)
#     if chat.get("type") != "group": fail(404, "NOT_FOUND", "Groupe introuvable.")
#     submitted_codes = data.employeeCodes or ([data.employeeCode] if data.employeeCode else [])
#     codes = list(dict.fromkeys(code.strip() for code in submitted_codes if code and code.strip()))
#     if not codes: fail(400, "VALIDATION_ERROR", "Sélectionnez au moins un salarié actif.")
#     members = [str(v) for v in (chat.get("memberIds") or [])]
#     changed = False
#     for code in codes:
#         target = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code.upper()))
#         if not target or not target.active: fail(404, "NOT_FOUND", f"Salarié actif introuvable : {code}.")
#         account = db.scalar(select(User).where(User.employee_code == target.employee_code, User.active.is_(True)))
#         if not account: fail(404, "NOT_FOUND", f"Compte actif introuvable pour le salarié {code}.")
#         if str(account.id) not in members:
#             members.append(str(account.id))
#             changed = True
#     if changed:
#         chat["memberIds"] = members
#         save(db, "chats", chat, key=chat_id)
#         _emit(db, "chat.updated", chat)
#     db.commit()
# @router.post("/chats/{chat_id}/join", status_code=204)
# def join_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
#     fail(403, "ADMIN_REQUIRED", "Seul un administrateur peut ajouter un salarié à un groupe.")


# @router.post("/chats/{chat_id}/leave", status_code=204)
# def leave_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
#     lock_domain(db, "chats", chat_id)
#     chat = _chat(db, chat_id)
#     if chat.get("type") != "group":
#         fail(404, "NOT_FOUND", "Groupe introuvable.")
#     chat["memberIds"] = [str(value) for value in (chat.get("memberIds") or []) if str(value) != _uid(user)]
#     save(db, "chats", chat, key=chat_id)
#     db.commit()
#     _emit(db, "chat.updated", chat)


# @router.get("/chats/{chat_id}/messages")
# def get_messages(
#     chat_id: str,
#     limit: int = Query(default=300, ge=1),
#     user: User = Depends(current_user),
#     db: Session = Depends(get_db),
# ):
#     chat = _readable_chat(db, chat_id, user)
#     del chat
#     messages = sorted(_message_rows(db, chat_id), key=lambda value: value.get("createdAt", ""))[-limit:]
#     return [_message_view(message) for message in messages]


# @router.post("/chats/{chat_id}/messages", status_code=204)
# def send_message(chat_id: str, data: MessageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
#     lock_domain(db, "chats", chat_id)
#     chat = _readable_chat(db, chat_id, user)
#     if chat.get("announcement") and _role(user) != "admin":
#         fail(403, "ANNOUNCEMENT_READ_ONLY", "Groupe d'annonces : seul l'administrateur peut écrire.")
#     text = _clean_required(data.text)
#     if not text:
#         fail(400, "VALIDATION_ERROR", "Le message est vide.")
#     if len(text) > MAX_MESSAGE_LENGTH:
#         fail(400, "VALIDATION_ERROR", f"Message trop long ({MAX_MESSAGE_LENGTH} caractères maximum).")

#     quoted = None
#     if data.replyToId:
#         candidate = row(db, "messages", data.replyToId)
#         if candidate and candidate.get("chatId") == chat_id:
#             quoted = {
#                 "id": str(candidate["id"]),
#                 "senderName": candidate.get("senderName", ""),
#                 "text": str(candidate.get("text", ""))[:MAX_REPLY_LENGTH],
#             }
#     sender_id, sender_name = _identity(db, user)
#     message = {
#         "id": new_id(),
#         "chatId": chat_id,
#         "senderId": sender_id,
#         "senderName": sender_name,
#         "senderRole": _role(user),
#         "text": text,
#         "createdAt": iso(now_utc()),
#         "replyTo": quoted,
#         "reactions": {},
#     }
#     save(db, "messages", message, key=message["id"])
#     chat.update(
#         lastMessageText=text[:120],
#         lastMessageAt=message["createdAt"],
#         lastMessageBy=sender_id,
#         lastMessageName=sender_name,
#     )
#     save(db, "chats", chat, key=chat_id)
#     _mark_read(db, chat, user)
#     db.commit()
#     _emit(db, "message.created", chat, extra={"messageId": message["id"]})


# @router.put("/chats/{chat_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
# def toggle_reaction(
#     chat_id: str,
#     message_id: str,
#     emoji: str,
#     user: User = Depends(current_user),
#     db: Session = Depends(get_db),
# ):
#     lock_domain(db, "chats", chat_id)
#     chat = _readable_chat(db, chat_id, user)
#     if emoji not in QUICK_REACTIONS:
#         fail(400, "VALIDATION_ERROR", "Réaction non autorisée.")
#     message = row(db, "messages", message_id)
#     if not message or message.get("chatId") != chat_id:
#         fail(404, "NOT_FOUND", "Message introuvable.")
#     reactions = dict(message.get("reactions") or {})
#     reacting_users = [str(value) for value in (reactions.get(emoji) or [])]
#     user_id = _uid(user)
#     if user_id in reacting_users:
#         reacting_users = [value for value in reacting_users if value != user_id]
#     else:
#         reacting_users.append(user_id)
#     if reacting_users:
#         reactions[emoji] = reacting_users
#     else:
#         reactions.pop(emoji, None)
#     message["reactions"] = reactions
#     save(db, "messages", message, key=message_id)
#     db.commit()
#     _emit(db, "message.reactions", chat, extra={"messageId": message_id})


# @router.delete("/chats/{chat_id}", status_code=204)
# def delete_chat(chat_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
#     lock_domain(db, "chats", chat_id)
#     chat = _chat(db, chat_id)
#     chat_pk = UUID(chat_id)
#     message_ids = select(ChatMessage.id).where(ChatMessage.chat_id == chat_pk)
#     db.execute(delete(MessageReaction).where(MessageReaction.message_id.in_(message_ids)))
#     db.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_pk))
#     db.execute(delete(ChatRead).where(ChatRead.chat_id == chat_pk))
#     db.execute(delete(ChatMember).where(ChatMember.chat_id == chat_pk))
#     chat_record = db.get(Chat, chat_pk)
#     if chat_record is not None:
#         db.delete(chat_record)
#     _emit(db, "chat.deleted", chat)
#     db.commit()


# @router.put("/chats/{chat_id}/read", status_code=204)
# def mark_seen(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
#     chat = _readable_chat(db, chat_id, user)
#     lock_domain(db, "reads", chat_id)
#     _mark_read(db, chat, user)
#     db.commit()
#     _emit(db, "chat.read", chat, extra={"userId": _uid(user)})


# @router.get("/chats/{chat_id}/reads")
# def get_reads(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
#     _readable_chat(db, chat_id, user)
#     return [_read_view(read) for read in rows(db, "reads") if read.get("chatId") == chat_id]


# # from __future__ import annotations

# # from typing import Any
# # from uuid import UUID

# # from fastapi import APIRouter, Depends, Query
# # from pydantic import BaseModel
# # from sqlalchemy import delete, func, select
# # from sqlalchemy.orm import Session

# # from .core import admin, current_user, employee, fail, get_db, user_payload
# # from .models import Chat, ChatMember, ChatMessage, ChatRead, Employee, MessageReaction, Role, User
# # from .store import emit_after_commit, iso, lock_domain, new_id, now_utc, row, rows, save


# # router = APIRouter(tags=["chat"])

# # QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
# # MAX_MESSAGE_LENGTH = 2000
# # MAX_REPLY_LENGTH = 140
# # MAX_GROUP_NAME_LENGTH = 60


# # class GroupIn(BaseModel):
# #     name: str
# #     announcement: bool = False


# # class GroupMemberIn(BaseModel):
# #     employeeCode: str
# # class DirectIn(BaseModel):
# #     employeeCode: str


# # class MessageIn(BaseModel):
# #     text: str
# #     replyToId: str | None = None


# # def _uid(user: User) -> str:
# #     return str(user.id)


# # def _role(user: User) -> str:
# #     return user.role.value if hasattr(user.role, "value") else str(user.role)


# # def _identity(db: Session, user: User) -> tuple[str, str]:
# #     """Return the display identity from the authenticated user, never from request data."""
# #     payload = user_payload(user, db)
# #     return _uid(user), payload.get("name") or "Administrateur"


# # def _chat(db: Session, chat_id: str) -> dict[str, Any]:
# #     value = row(db, "chats", chat_id)
# #     if value is None:
# #         fail(404, "NOT_FOUND", "Conversation introuvable.")
# #     return value


# # def _can_see(chat: dict[str, Any], user: User) -> bool:
# #     if _role(user) == "admin":
# #         return True
# #     return chat.get("type") == "group" or chat.get("directUserId") == _uid(user)


# # def _can_read(chat: dict[str, Any], user: User) -> bool:
# #     return _role(user) == "admin" or _uid(user) in (chat.get("memberIds") or [])


# # def _readable_chat(db: Session, chat_id: str, user: User) -> dict[str, Any]:
# #     chat = _chat(db, chat_id)
# #     if not _can_read(chat, user):
# #         fail(403, "FORBIDDEN", "Vous ne faites pas partie de cette conversation.")
# #     return chat


# # def _active_admin_ids(db: Session) -> set[str]:
# #     return {
# #         str(value.id)
# #         for value in db.scalars(select(User).where(User.role == Role.admin, User.active.is_(True)))
# #     }


# # def _recipients(db: Session, chat: dict[str, Any]) -> set[str]:
# #     # Tous les salariés voient les groupes dans l'annuaire et peuvent les rejoindre.
# #     # Les directs restent limités aux administrateurs et au salarié concerné.
# #     recipients = _active_admin_ids(db) | {str(value) for value in (chat.get("memberIds") or [])}
# #     if chat.get("type") == "group":
# #         recipients.update(str(item.id) for item in db.scalars(select(User).where(User.role == Role.employee, User.active.is_(True))))
# #     return recipients


# # def _emit(db: Session, event_type: str, chat: dict[str, Any], *, extra: dict[str, Any] | None = None) -> None:
# #     event: dict[str, Any] = {"type": event_type, "chatId": str(chat["id"])}
# #     if extra:
# #         event.update(extra)
# #     recipients = _recipients(db, chat)
# #     try:
# #         emit_after_commit(db, event, recipients)
# #     except ImportError:
# #         # Older app.main versions do not declare the queue yet; keep the
# #         # store contract intact and create the queue lazily for this process.
# #         from . import main

# #         if not hasattr(main, "event_queue"):
# #             main.event_queue = []
# #         emit_after_commit(db, event, recipients)


# # def _message_rows(db: Session, chat_id: str) -> list[dict[str, Any]]:
# #     return [value for value in rows(db, "messages") if value.get("chatId") == chat_id]


# # def _read_row(db: Session, chat_id: str, user_id: str) -> dict[str, Any] | None:
# #     return row(db, "reads", f"{chat_id}:{user_id}")


# # def _mark_read(db: Session, chat: dict[str, Any], user: User) -> None:
# #     user_id, name = _identity(db, user)
# #     save(
# #         db,
# #         "reads",
# #         {"chatId": chat["id"], "userId": user_id, "name": name, "role": _role(user), "seenAt": iso(now_utc())},
# #         key=f"{chat['id']}:{user_id}",
# #     )


# # def _preview_from_messages(db: Session, chat: dict[str, Any]) -> None:
# #     messages = _message_rows(db, str(chat["id"]))
# #     latest = max(messages, key=lambda value: value.get("createdAt", ""), default=None)
# #     if latest is None:
# #         chat.update(lastMessageText=None, lastMessageAt=None, lastMessageBy=None, lastMessageName=None)
# #     else:
# #         chat.update(
# #             lastMessageText=str(latest.get("text", ""))[:120],
# #             lastMessageAt=latest.get("createdAt"),
# #             lastMessageBy=latest.get("senderId"),
# #             lastMessageName=latest.get("senderName"),
# #         )
# #     save(db, "chats", chat, key=str(chat["id"]))


# # def _chat_view(db: Session, chat: dict[str, Any], user: User) -> dict[str, Any]:
# #     user_id = _uid(user)
# #     seen = (_read_row(db, str(chat["id"]), user_id) or {}).get("seenAt", "")
# #     unread = 0
# #     if _can_read(chat, user):
# #         unread = sum(
# #             1
# #             for message in _message_rows(db, str(chat["id"]))
# #             if message.get("senderId") != user_id and message.get("createdAt", "") > seen
# #         )

# #     direct_name: str | None = None
# #     direct_user_id = chat.get("directUserId")
# #     if direct_user_id:
# #         direct_user = db.get(User, direct_user_id)
# #         if direct_user and direct_user.employee_code:
# #             direct_employee = db.get(Employee, direct_user.employee_code)
# #             if direct_employee:
# #                 direct_name = f"{direct_employee.first_name} {direct_employee.last_name}"

# #     return {
# #         "id": str(chat["id"]),
# #         "type": chat.get("type"),
# #         "name": chat.get("name", ""),
# #         "memberIds": [str(value) for value in (chat.get("memberIds") or [])],
# #         "createdAt": chat.get("createdAt"),
# #         "announcement": bool(chat.get("announcement", False)),
# #         "directUserId": direct_user_id,
# #         "directEmployeeName": direct_name,
# #         "lastMessageText": chat.get("lastMessageText"),
# #         "lastMessageAt": chat.get("lastMessageAt"),
# #         "lastMessageBy": chat.get("lastMessageBy"),
# #         "lastMessageName": chat.get("lastMessageName"),
# #         "unreadCount": unread,
# #     }


# # def _clean_required(value: Any) -> str:
# #     return value.strip() if isinstance(value, str) else ""


# # def _message_view(message: dict[str, Any]) -> dict[str, Any]:
# #     return {
# #         "id": str(message["id"]),
# #         "senderId": message.get("senderId"),
# #         "senderName": message.get("senderName", ""),
# #         "senderRole": message.get("senderRole"),
# #         "text": message.get("text", ""),
# #         "createdAt": message.get("createdAt"),
# #         "replyTo": message.get("replyTo"),
# #         "reactions": message.get("reactions") or {},
# #     }


# # def _read_view(read: dict[str, Any]) -> dict[str, Any]:
# #     return {
# #         "userId": str(read.get("userId")),
# #         "name": read.get("name", ""),
# #         "role": read.get("role"),
# #         "seenAt": read.get("seenAt"),
# #     }


# # @router.get("/chats")
# # def list_chats(user: User = Depends(current_user), db: Session = Depends(get_db)):
# #     return [_chat_view(db, chat, user) for chat in rows(db, "chats") if _can_see(chat, user)]


# # @router.post("/chats/groups")
# # def create_group(data: GroupIn, user: User = Depends(admin), db: Session = Depends(get_db)):
# #     name = _clean_required(data.name)
# #     if not name:
# #         fail(400, "VALIDATION_ERROR", "Le nom du groupe est obligatoire.")
# #     lock_domain(db, "chats")
# #     chat = {
# #         "id": new_id(),
# #         "type": "group",
# #         "name": name[:MAX_GROUP_NAME_LENGTH],
# #         "announcement": data.announcement is True,
# #         "memberIds": [],
# #         "createdAt": iso(now_utc()),
# #         "directUserId": None,
# #         "directEmployeeName": None,
# #         "lastMessageText": None,
# #         "lastMessageAt": None,
# #         "lastMessageBy": None,
# #         "lastMessageName": None,
# #     }
# #     save(db, "chats", chat, key=chat["id"])
# #     db.commit()
# #     _emit(db, "chat.updated", chat)
# #     return {"id": chat["id"]}


# # @router.post("/chats/direct")
# # def open_direct(data: DirectIn, user: User = Depends(admin), db: Session = Depends(get_db)):
# #     code = _clean_required(data.employeeCode).upper()
# #     if not code:
# #         fail(404, "NOT_FOUND", "Salarié introuvable.")
# #     lock_domain(db, "chats", f"direct:{code}")
# #     target_employee = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code))
# #     if not target_employee:
# #         fail(404, "NOT_FOUND", "Salarié introuvable.")
# #     target_user = db.scalar(select(User).where(User.employee_code == target_employee.employee_code))
# #     if not target_user:
# #         fail(404, "NOT_FOUND", "Ce salarié n'a pas de compte de connexion.")
# #     target_id = str(target_user.id)
# #     existing = next(
# #         (chat for chat in rows(db, "chats") if chat.get("type") == "direct" and chat.get("directUserId") == target_id),
# #         None,
# #     )
# #     if existing:
# #         return {"id": existing["id"]}

# #     chat = {
# #         "id": new_id(),
# #         "type": "direct",
# #         "name": "",
# #         "announcement": False,
# #         "memberIds": [target_id],
# #         "createdAt": iso(now_utc()),
# #         "directUserId": target_id,
# #         "directEmployeeName": f"{target_employee.first_name} {target_employee.last_name}",
# #         "lastMessageText": None,
# #         "lastMessageAt": None,
# #         "lastMessageBy": None,
# #         "lastMessageName": None,
# #     }
# #     save(db, "chats", chat, key=chat["id"])
# #     db.commit()
# #     _emit(db, "chat.updated", chat)
# #     return {"id": chat["id"]}


# # @router.post("/chats/{chat_id}/members", status_code=204)
# # def add_group_member(chat_id: str, data: GroupMemberIn, user: User = Depends(admin), db: Session = Depends(get_db)):
# #     lock_domain(db, "chats", chat_id)
# #     chat = _chat(db, chat_id)
# #     if chat.get("type") != "group": fail(404, "NOT_FOUND", "Groupe introuvable.")
# #     code = data.employeeCode.strip()
# #     target = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code.upper()))
# #     if not target or not target.active: fail(404, "NOT_FOUND", "Salarié actif introuvable.")
# #     account = db.scalar(select(User).where(User.employee_code == target.employee_code, User.active.is_(True)))
# #     if not account: fail(404, "NOT_FOUND", "Compte actif introuvable pour ce salarié.")
# #     members = [str(v) for v in (chat.get("memberIds") or [])]
# #     if str(account.id) not in members:
# #         members.append(str(account.id)); chat["memberIds"] = members; save(db, "chats", chat, key=chat_id); db.commit(); _emit(db, "chat.updated", chat)
# # @router.post("/chats/{chat_id}/join", status_code=204)
# # def join_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
# #     lock_domain(db, "chats", chat_id)
# #     chat = _chat(db, chat_id)
# #     if chat.get("type") != "group":
# #         fail(404, "NOT_FOUND", "Groupe introuvable.")
# #     members = [str(value) for value in (chat.get("memberIds") or [])]
# #     if _uid(user) not in members:
# #         members.append(_uid(user))
# #         chat["memberIds"] = members
# #         save(db, "chats", chat, key=chat_id)
# #     db.commit()
# #     _emit(db, "chat.updated", chat)


# # @router.post("/chats/{chat_id}/leave", status_code=204)
# # def leave_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
# #     lock_domain(db, "chats", chat_id)
# #     chat = _chat(db, chat_id)
# #     if chat.get("type") != "group":
# #         fail(404, "NOT_FOUND", "Groupe introuvable.")
# #     chat["memberIds"] = [str(value) for value in (chat.get("memberIds") or []) if str(value) != _uid(user)]
# #     save(db, "chats", chat, key=chat_id)
# #     db.commit()
# #     _emit(db, "chat.updated", chat)


# # @router.get("/chats/{chat_id}/messages")
# # def get_messages(
# #     chat_id: str,
# #     limit: int = Query(default=300, ge=1),
# #     user: User = Depends(current_user),
# #     db: Session = Depends(get_db),
# # ):
# #     chat = _readable_chat(db, chat_id, user)
# #     del chat
# #     messages = sorted(_message_rows(db, chat_id), key=lambda value: value.get("createdAt", ""))[-limit:]
# #     return [_message_view(message) for message in messages]


# # @router.post("/chats/{chat_id}/messages", status_code=204)
# # def send_message(chat_id: str, data: MessageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
# #     lock_domain(db, "chats", chat_id)
# #     chat = _readable_chat(db, chat_id, user)
# #     if chat.get("announcement") and _role(user) != "admin":
# #         fail(403, "ANNOUNCEMENT_READ_ONLY", "Groupe d'annonces : seul l'administrateur peut écrire.")
# #     text = _clean_required(data.text)
# #     if not text:
# #         fail(400, "VALIDATION_ERROR", "Le message est vide.")
# #     if len(text) > MAX_MESSAGE_LENGTH:
# #         fail(400, "VALIDATION_ERROR", f"Message trop long ({MAX_MESSAGE_LENGTH} caractères maximum).")

# #     quoted = None
# #     if data.replyToId:
# #         candidate = row(db, "messages", data.replyToId)
# #         if candidate and candidate.get("chatId") == chat_id:
# #             quoted = {
# #                 "id": str(candidate["id"]),
# #                 "senderName": candidate.get("senderName", ""),
# #                 "text": str(candidate.get("text", ""))[:MAX_REPLY_LENGTH],
# #             }
# #     sender_id, sender_name = _identity(db, user)
# #     message = {
# #         "id": new_id(),
# #         "chatId": chat_id,
# #         "senderId": sender_id,
# #         "senderName": sender_name,
# #         "senderRole": _role(user),
# #         "text": text,
# #         "createdAt": iso(now_utc()),
# #         "replyTo": quoted,
# #         "reactions": {},
# #     }
# #     save(db, "messages", message, key=message["id"])
# #     chat.update(
# #         lastMessageText=text[:120],
# #         lastMessageAt=message["createdAt"],
# #         lastMessageBy=sender_id,
# #         lastMessageName=sender_name,
# #     )
# #     save(db, "chats", chat, key=chat_id)
# #     _mark_read(db, chat, user)
# #     db.commit()
# #     _emit(db, "message.created", chat, extra={"messageId": message["id"]})


# # @router.put("/chats/{chat_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
# # def toggle_reaction(
# #     chat_id: str,
# #     message_id: str,
# #     emoji: str,
# #     user: User = Depends(current_user),
# #     db: Session = Depends(get_db),
# # ):
# #     lock_domain(db, "chats", chat_id)
# #     chat = _readable_chat(db, chat_id, user)
# #     if emoji not in QUICK_REACTIONS:
# #         fail(400, "VALIDATION_ERROR", "Réaction non autorisée.")
# #     message = row(db, "messages", message_id)
# #     if not message or message.get("chatId") != chat_id:
# #         fail(404, "NOT_FOUND", "Message introuvable.")
# #     reactions = dict(message.get("reactions") or {})
# #     reacting_users = [str(value) for value in (reactions.get(emoji) or [])]
# #     user_id = _uid(user)
# #     if user_id in reacting_users:
# #         reacting_users = [value for value in reacting_users if value != user_id]
# #     else:
# #         reacting_users.append(user_id)
# #     if reacting_users:
# #         reactions[emoji] = reacting_users
# #     else:
# #         reactions.pop(emoji, None)
# #     message["reactions"] = reactions
# #     save(db, "messages", message, key=message_id)
# #     db.commit()
# #     _emit(db, "message.reactions", chat, extra={"messageId": message_id})


# # @router.delete("/chats/{chat_id}", status_code=204)
# # def delete_chat(chat_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
# #     lock_domain(db, "chats", chat_id)
# #     chat = _chat(db, chat_id)
# #     chat_pk = UUID(chat_id)
# #     message_ids = select(ChatMessage.id).where(ChatMessage.chat_id == chat_pk)
# #     db.execute(delete(MessageReaction).where(MessageReaction.message_id.in_(message_ids)))
# #     db.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_pk))
# #     db.execute(delete(ChatRead).where(ChatRead.chat_id == chat_pk))
# #     db.execute(delete(ChatMember).where(ChatMember.chat_id == chat_pk))
# #     chat_record = db.get(Chat, chat_pk)
# #     if chat_record is not None:
# #         db.delete(chat_record)
# #     _emit(db, "chat.deleted", chat)
# #     db.commit()


# # @router.put("/chats/{chat_id}/read", status_code=204)
# # def mark_seen(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
# #     chat = _readable_chat(db, chat_id, user)
# #     lock_domain(db, "reads", chat_id)
# #     _mark_read(db, chat, user)
# #     db.commit()
# #     _emit(db, "chat.read", chat, extra={"userId": _uid(user)})


# # @router.get("/chats/{chat_id}/reads")
# # def get_reads(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
# #     _readable_chat(db, chat_id, user)
# #     return [_read_view(read) for read in rows(db, "reads") if read.get("chatId") == chat_id]


# # # from __future__ import annotations

# # # from typing import Any

# # # from fastapi import APIRouter, Depends, Query
# # # from pydantic import BaseModel
# # # from sqlalchemy import func, select
# # # from sqlalchemy.orm import Session

# # # from .core import admin, current_user, employee, fail, get_db, user_payload
# # # from .models import Employee, Role, User
# # # from .store import emit_after_commit, iso, lock_domain, new_id, now_utc, remove, row, rows, save


# # # router = APIRouter(tags=["chat"])

# # # QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
# # # MAX_MESSAGE_LENGTH = 2000
# # # MAX_REPLY_LENGTH = 140
# # # MAX_GROUP_NAME_LENGTH = 60


# # # class GroupIn(BaseModel):
# # #     name: str
# # #     announcement: bool = False


# # # class GroupMemberIn(BaseModel):
# # #     employeeCode: str
# # # class DirectIn(BaseModel):
# # #     employeeCode: str


# # # class MessageIn(BaseModel):
# # #     text: str
# # #     replyToId: str | None = None


# # # def _uid(user: User) -> str:
# # #     return str(user.id)


# # # def _role(user: User) -> str:
# # #     return user.role.value if hasattr(user.role, "value") else str(user.role)


# # # def _identity(db: Session, user: User) -> tuple[str, str]:
# # #     """Return the display identity from the authenticated user, never from request data."""
# # #     payload = user_payload(user, db)
# # #     return _uid(user), payload.get("name") or "Administrateur"


# # # def _chat(db: Session, chat_id: str) -> dict[str, Any]:
# # #     value = row(db, "chats", chat_id)
# # #     if value is None:
# # #         fail(404, "NOT_FOUND", "Conversation introuvable.")
# # #     return value


# # # def _can_see(chat: dict[str, Any], user: User) -> bool:
# # #     if _role(user) == "admin":
# # #         return True
# # #     return chat.get("type") == "group" or chat.get("directUserId") == _uid(user)


# # # def _can_read(chat: dict[str, Any], user: User) -> bool:
# # #     return _role(user) == "admin" or _uid(user) in (chat.get("memberIds") or [])


# # # def _readable_chat(db: Session, chat_id: str, user: User) -> dict[str, Any]:
# # #     chat = _chat(db, chat_id)
# # #     if not _can_read(chat, user):
# # #         fail(403, "FORBIDDEN", "Vous ne faites pas partie de cette conversation.")
# # #     return chat


# # # def _active_admin_ids(db: Session) -> set[str]:
# # #     return {
# # #         str(value.id)
# # #         for value in db.scalars(select(User).where(User.role == Role.admin, User.active.is_(True)))
# # #     }


# # # def _recipients(db: Session, chat: dict[str, Any]) -> set[str]:
# # #     # Tous les salariés voient les groupes dans l'annuaire et peuvent les rejoindre.
# # #     # Les directs restent limités aux administrateurs et au salarié concerné.
# # #     recipients = _active_admin_ids(db) | {str(value) for value in (chat.get("memberIds") or [])}
# # #     if chat.get("type") == "group":
# # #         recipients.update(str(item.id) for item in db.scalars(select(User).where(User.role == Role.employee, User.active.is_(True))))
# # #     return recipients


# # # def _emit(db: Session, event_type: str, chat: dict[str, Any], *, extra: dict[str, Any] | None = None) -> None:
# # #     event: dict[str, Any] = {"type": event_type, "chatId": str(chat["id"])}
# # #     if extra:
# # #         event.update(extra)
# # #     recipients = _recipients(db, chat)
# # #     try:
# # #         emit_after_commit(db, event, recipients)
# # #     except ImportError:
# # #         # Older app.main versions do not declare the queue yet; keep the
# # #         # store contract intact and create the queue lazily for this process.
# # #         from . import main

# # #         if not hasattr(main, "event_queue"):
# # #             main.event_queue = []
# # #         emit_after_commit(db, event, recipients)


# # # def _message_rows(db: Session, chat_id: str) -> list[dict[str, Any]]:
# # #     return [value for value in rows(db, "messages") if value.get("chatId") == chat_id]


# # # def _read_row(db: Session, chat_id: str, user_id: str) -> dict[str, Any] | None:
# # #     return row(db, "reads", f"{chat_id}:{user_id}")


# # # def _mark_read(db: Session, chat: dict[str, Any], user: User) -> None:
# # #     user_id, name = _identity(db, user)
# # #     save(
# # #         db,
# # #         "reads",
# # #         {"chatId": chat["id"], "userId": user_id, "name": name, "role": _role(user), "seenAt": iso(now_utc())},
# # #         key=f"{chat['id']}:{user_id}",
# # #     )


# # # def _preview_from_messages(db: Session, chat: dict[str, Any]) -> None:
# # #     messages = _message_rows(db, str(chat["id"]))
# # #     latest = max(messages, key=lambda value: value.get("createdAt", ""), default=None)
# # #     if latest is None:
# # #         chat.update(lastMessageText=None, lastMessageAt=None, lastMessageBy=None, lastMessageName=None)
# # #     else:
# # #         chat.update(
# # #             lastMessageText=str(latest.get("text", ""))[:120],
# # #             lastMessageAt=latest.get("createdAt"),
# # #             lastMessageBy=latest.get("senderId"),
# # #             lastMessageName=latest.get("senderName"),
# # #         )
# # #     save(db, "chats", chat, key=str(chat["id"]))


# # # def _chat_view(db: Session, chat: dict[str, Any], user: User) -> dict[str, Any]:
# # #     user_id = _uid(user)
# # #     seen = (_read_row(db, str(chat["id"]), user_id) or {}).get("seenAt", "")
# # #     unread = 0
# # #     if _can_read(chat, user):
# # #         unread = sum(
# # #             1
# # #             for message in _message_rows(db, str(chat["id"]))
# # #             if message.get("senderId") != user_id and message.get("createdAt", "") > seen
# # #         )

# # #     direct_name: str | None = None
# # #     direct_user_id = chat.get("directUserId")
# # #     if direct_user_id:
# # #         direct_user = db.get(User, direct_user_id)
# # #         if direct_user and direct_user.employee_code:
# # #             direct_employee = db.get(Employee, direct_user.employee_code)
# # #             if direct_employee:
# # #                 direct_name = f"{direct_employee.first_name} {direct_employee.last_name}"

# # #     return {
# # #         "id": str(chat["id"]),
# # #         "type": chat.get("type"),
# # #         "name": chat.get("name", ""),
# # #         "memberIds": [str(value) for value in (chat.get("memberIds") or [])],
# # #         "createdAt": chat.get("createdAt"),
# # #         "announcement": bool(chat.get("announcement", False)),
# # #         "directUserId": direct_user_id,
# # #         "directEmployeeName": direct_name,
# # #         "lastMessageText": chat.get("lastMessageText"),
# # #         "lastMessageAt": chat.get("lastMessageAt"),
# # #         "lastMessageBy": chat.get("lastMessageBy"),
# # #         "lastMessageName": chat.get("lastMessageName"),
# # #         "unreadCount": unread,
# # #     }


# # # def _clean_required(value: Any) -> str:
# # #     return value.strip() if isinstance(value, str) else ""


# # # def _message_view(message: dict[str, Any]) -> dict[str, Any]:
# # #     return {
# # #         "id": str(message["id"]),
# # #         "senderId": message.get("senderId"),
# # #         "senderName": message.get("senderName", ""),
# # #         "senderRole": message.get("senderRole"),
# # #         "text": message.get("text", ""),
# # #         "createdAt": message.get("createdAt"),
# # #         "replyTo": message.get("replyTo"),
# # #         "reactions": message.get("reactions") or {},
# # #     }


# # # def _read_view(read: dict[str, Any]) -> dict[str, Any]:
# # #     return {
# # #         "userId": str(read.get("userId")),
# # #         "name": read.get("name", ""),
# # #         "role": read.get("role"),
# # #         "seenAt": read.get("seenAt"),
# # #     }


# # # @router.get("/chats")
# # # def list_chats(user: User = Depends(current_user), db: Session = Depends(get_db)):
# # #     return [_chat_view(db, chat, user) for chat in rows(db, "chats") if _can_see(chat, user)]


# # # @router.post("/chats/groups")
# # # def create_group(data: GroupIn, user: User = Depends(admin), db: Session = Depends(get_db)):
# # #     name = _clean_required(data.name)
# # #     if not name:
# # #         fail(400, "VALIDATION_ERROR", "Le nom du groupe est obligatoire.")
# # #     lock_domain(db, "chats")
# # #     chat = {
# # #         "id": new_id(),
# # #         "type": "group",
# # #         "name": name[:MAX_GROUP_NAME_LENGTH],
# # #         "announcement": data.announcement is True,
# # #         "memberIds": [],
# # #         "createdAt": iso(now_utc()),
# # #         "directUserId": None,
# # #         "directEmployeeName": None,
# # #         "lastMessageText": None,
# # #         "lastMessageAt": None,
# # #         "lastMessageBy": None,
# # #         "lastMessageName": None,
# # #     }
# # #     save(db, "chats", chat, key=chat["id"])
# # #     db.commit()
# # #     _emit(db, "chat.updated", chat)
# # #     return {"id": chat["id"]}


# # # @router.post("/chats/direct")
# # # def open_direct(data: DirectIn, user: User = Depends(admin), db: Session = Depends(get_db)):
# # #     code = _clean_required(data.employeeCode).upper()
# # #     if not code:
# # #         fail(404, "NOT_FOUND", "Salarié introuvable.")
# # #     lock_domain(db, "chats", f"direct:{code}")
# # #     target_employee = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code))
# # #     if not target_employee:
# # #         fail(404, "NOT_FOUND", "Salarié introuvable.")
# # #     target_user = db.scalar(select(User).where(User.employee_code == target_employee.employee_code))
# # #     if not target_user:
# # #         fail(404, "NOT_FOUND", "Ce salarié n'a pas de compte de connexion.")
# # #     target_id = str(target_user.id)
# # #     existing = next(
# # #         (chat for chat in rows(db, "chats") if chat.get("type") == "direct" and chat.get("directUserId") == target_id),
# # #         None,
# # #     )
# # #     if existing:
# # #         return {"id": existing["id"]}

# # #     chat = {
# # #         "id": new_id(),
# # #         "type": "direct",
# # #         "name": "",
# # #         "announcement": False,
# # #         "memberIds": [target_id],
# # #         "createdAt": iso(now_utc()),
# # #         "directUserId": target_id,
# # #         "directEmployeeName": f"{target_employee.first_name} {target_employee.last_name}",
# # #         "lastMessageText": None,
# # #         "lastMessageAt": None,
# # #         "lastMessageBy": None,
# # #         "lastMessageName": None,
# # #     }
# # #     save(db, "chats", chat, key=chat["id"])
# # #     db.commit()
# # #     _emit(db, "chat.updated", chat)
# # #     return {"id": chat["id"]}


# # # @router.post("/chats/{chat_id}/members", status_code=204)
# # # def add_group_member(chat_id: str, data: GroupMemberIn, user: User = Depends(admin), db: Session = Depends(get_db)):
# # #     lock_domain(db, "chats", chat_id)
# # #     chat = _chat(db, chat_id)
# # #     if chat.get("type") != "group": fail(404, "NOT_FOUND", "Groupe introuvable.")
# # #     code = data.employeeCode.strip()
# # #     target = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code.upper()))
# # #     if not target or not target.active: fail(404, "NOT_FOUND", "Salarié actif introuvable.")
# # #     account = db.scalar(select(User).where(User.employee_code == target.employee_code, User.active.is_(True)))
# # #     if not account: fail(404, "NOT_FOUND", "Compte actif introuvable pour ce salarié.")
# # #     members = [str(v) for v in (chat.get("memberIds") or [])]
# # #     if str(account.id) not in members:
# # #         members.append(str(account.id)); chat["memberIds"] = members; save(db, "chats", chat, key=chat_id); db.commit(); _emit(db, "chat.updated", chat)
# # # @router.post("/chats/{chat_id}/join", status_code=204)
# # # def join_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
# # #     lock_domain(db, "chats", chat_id)
# # #     chat = _chat(db, chat_id)
# # #     if chat.get("type") != "group":
# # #         fail(404, "NOT_FOUND", "Groupe introuvable.")
# # #     members = [str(value) for value in (chat.get("memberIds") or [])]
# # #     if _uid(user) not in members:
# # #         members.append(_uid(user))
# # #         chat["memberIds"] = members
# # #         save(db, "chats", chat, key=chat_id)
# # #     db.commit()
# # #     _emit(db, "chat.updated", chat)


# # # @router.post("/chats/{chat_id}/leave", status_code=204)
# # # def leave_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
# # #     lock_domain(db, "chats", chat_id)
# # #     chat = _chat(db, chat_id)
# # #     if chat.get("type") != "group":
# # #         fail(404, "NOT_FOUND", "Groupe introuvable.")
# # #     chat["memberIds"] = [str(value) for value in (chat.get("memberIds") or []) if str(value) != _uid(user)]
# # #     save(db, "chats", chat, key=chat_id)
# # #     db.commit()
# # #     _emit(db, "chat.updated", chat)


# # # @router.get("/chats/{chat_id}/messages")
# # # def get_messages(
# # #     chat_id: str,
# # #     limit: int = Query(default=300, ge=1),
# # #     user: User = Depends(current_user),
# # #     db: Session = Depends(get_db),
# # # ):
# # #     chat = _readable_chat(db, chat_id, user)
# # #     del chat
# # #     messages = sorted(_message_rows(db, chat_id), key=lambda value: value.get("createdAt", ""))[-limit:]
# # #     return [_message_view(message) for message in messages]


# # # @router.post("/chats/{chat_id}/messages", status_code=204)
# # # def send_message(chat_id: str, data: MessageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
# # #     lock_domain(db, "chats", chat_id)
# # #     chat = _readable_chat(db, chat_id, user)
# # #     if chat.get("announcement") and _role(user) != "admin":
# # #         fail(403, "ANNOUNCEMENT_READ_ONLY", "Groupe d'annonces : seul l'administrateur peut écrire.")
# # #     text = _clean_required(data.text)
# # #     if not text:
# # #         fail(400, "VALIDATION_ERROR", "Le message est vide.")
# # #     if len(text) > MAX_MESSAGE_LENGTH:
# # #         fail(400, "VALIDATION_ERROR", f"Message trop long ({MAX_MESSAGE_LENGTH} caractères maximum).")

# # #     quoted = None
# # #     if data.replyToId:
# # #         candidate = row(db, "messages", data.replyToId)
# # #         if candidate and candidate.get("chatId") == chat_id:
# # #             quoted = {
# # #                 "id": str(candidate["id"]),
# # #                 "senderName": candidate.get("senderName", ""),
# # #                 "text": str(candidate.get("text", ""))[:MAX_REPLY_LENGTH],
# # #             }
# # #     sender_id, sender_name = _identity(db, user)
# # #     message = {
# # #         "id": new_id(),
# # #         "chatId": chat_id,
# # #         "senderId": sender_id,
# # #         "senderName": sender_name,
# # #         "senderRole": _role(user),
# # #         "text": text,
# # #         "createdAt": iso(now_utc()),
# # #         "replyTo": quoted,
# # #         "reactions": {},
# # #     }
# # #     save(db, "messages", message, key=message["id"])
# # #     chat.update(
# # #         lastMessageText=text[:120],
# # #         lastMessageAt=message["createdAt"],
# # #         lastMessageBy=sender_id,
# # #         lastMessageName=sender_name,
# # #     )
# # #     save(db, "chats", chat, key=chat_id)
# # #     _mark_read(db, chat, user)
# # #     db.commit()
# # #     _emit(db, "message.created", chat, extra={"messageId": message["id"]})


# # # @router.put("/chats/{chat_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
# # # def toggle_reaction(
# # #     chat_id: str,
# # #     message_id: str,
# # #     emoji: str,
# # #     user: User = Depends(current_user),
# # #     db: Session = Depends(get_db),
# # # ):
# # #     lock_domain(db, "chats", chat_id)
# # #     chat = _readable_chat(db, chat_id, user)
# # #     if emoji not in QUICK_REACTIONS:
# # #         fail(400, "VALIDATION_ERROR", "Réaction non autorisée.")
# # #     message = row(db, "messages", message_id)
# # #     if not message or message.get("chatId") != chat_id:
# # #         fail(404, "NOT_FOUND", "Message introuvable.")
# # #     reactions = dict(message.get("reactions") or {})
# # #     reacting_users = [str(value) for value in (reactions.get(emoji) or [])]
# # #     user_id = _uid(user)
# # #     if user_id in reacting_users:
# # #         reacting_users = [value for value in reacting_users if value != user_id]
# # #     else:
# # #         reacting_users.append(user_id)
# # #     if reacting_users:
# # #         reactions[emoji] = reacting_users
# # #     else:
# # #         reactions.pop(emoji, None)
# # #     message["reactions"] = reactions
# # #     save(db, "messages", message, key=message_id)
# # #     db.commit()
# # #     _emit(db, "message.reactions", chat, extra={"messageId": message_id})


# # # @router.delete("/chats/{chat_id}", status_code=204)
# # # def delete_chat(chat_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
# # #     lock_domain(db, "chats", chat_id)
# # #     chat = _chat(db, "chats", chat_id)
# # #     remove(db, "chats", chat_id)
# # #     for message in _message_rows(db, chat_id):
# # #         remove(db, "messages", str(message["id"]))
# # #     for read in rows(db, "reads"):
# # #         if read.get("chatId") == chat_id:
# # #             remove(db, "reads", f"{chat_id}:{read.get('userId')}")
# # #     db.commit()
# # #     _emit(db, "chat.deleted", chat)


# # # @router.put("/chats/{chat_id}/read", status_code=204)
# # # def mark_seen(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
# # #     chat = _readable_chat(db, chat_id, user)
# # #     lock_domain(db, "reads", chat_id)
# # #     _mark_read(db, chat, user)
# # #     db.commit()
# # #     _emit(db, "chat.read", chat, extra={"userId": _uid(user)})


# # # @router.get("/chats/{chat_id}/reads")
# # # def get_reads(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
# # #     _readable_chat(db, chat_id, user)
# # #     return [_read_view(read) for read in rows(db, "reads") if read.get("chatId") == chat_id]


# # # # from __future__ import annotations

# # # # from typing import Any

# # # # from fastapi import APIRouter, Depends, Query
# # # # from pydantic import BaseModel
# # # # from sqlalchemy import func, select
# # # # from sqlalchemy.orm import Session

# # # # from .core import admin, current_user, employee, fail, get_db, user_payload
# # # # from .models import Employee, Role, User
# # # # from .store import emit_after_commit, iso, lock_domain, new_id, now_utc, remove, row, rows, save


# # # # router = APIRouter(tags=["chat"])

# # # # QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
# # # # MAX_MESSAGE_LENGTH = 2000
# # # # MAX_REPLY_LENGTH = 140
# # # # MAX_GROUP_NAME_LENGTH = 60


# # # # class GroupIn(BaseModel):
# # # #     name: str
# # # #     announcement: bool = False


# # # # class DirectIn(BaseModel):
# # # #     employeeCode: str


# # # # class MessageIn(BaseModel):
# # # #     text: str
# # # #     replyToId: str | None = None


# # # # def _uid(user: User) -> str:
# # # #     return str(user.id)


# # # # def _role(user: User) -> str:
# # # #     return user.role.value if hasattr(user.role, "value") else str(user.role)


# # # # def _identity(db: Session, user: User) -> tuple[str, str]:
# # # #     """Return the display identity from the authenticated user, never from request data."""
# # # #     payload = user_payload(user, db)
# # # #     return _uid(user), payload.get("name") or "Administrateur"


# # # # def _chat(db: Session, chat_id: str) -> dict[str, Any]:
# # # #     value = row(db, "chats", chat_id)
# # # #     if value is None:
# # # #         fail(404, "NOT_FOUND", "Conversation introuvable.")
# # # #     return value


# # # # def _can_see(chat: dict[str, Any], user: User) -> bool:
# # # #     if _role(user) == "admin":
# # # #         return True
# # # #     return chat.get("type") == "group" or chat.get("directUserId") == _uid(user)


# # # # def _can_read(chat: dict[str, Any], user: User) -> bool:
# # # #     return _role(user) == "admin" or _uid(user) in (chat.get("memberIds") or [])


# # # # def _readable_chat(db: Session, chat_id: str, user: User) -> dict[str, Any]:
# # # #     chat = _chat(db, chat_id)
# # # #     if not _can_read(chat, user):
# # # #         fail(403, "FORBIDDEN", "Vous ne faites pas partie de cette conversation.")
# # # #     return chat


# # # # def _active_admin_ids(db: Session) -> set[str]:
# # # #     return {
# # # #         str(value.id)
# # # #         for value in db.scalars(select(User).where(User.role == Role.admin, User.active.is_(True)))
# # # #     }


# # # # def _recipients(db: Session, chat: dict[str, Any]) -> set[str]:
# # # #     # Tous les salariés voient les groupes dans l'annuaire et peuvent les rejoindre.
# # # #     # Les directs restent limités aux administrateurs et au salarié concerné.
# # # #     recipients = _active_admin_ids(db) | {str(value) for value in (chat.get("memberIds") or [])}
# # # #     if chat.get("type") == "group":
# # # #         recipients.update(str(item.id) for item in db.scalars(select(User).where(User.role == Role.employee, User.active.is_(True))))
# # # #     return recipients


# # # # def _emit(db: Session, event_type: str, chat: dict[str, Any], *, extra: dict[str, Any] | None = None) -> None:
# # # #     event: dict[str, Any] = {"type": event_type, "chatId": str(chat["id"])}
# # # #     if extra:
# # # #         event.update(extra)
# # # #     recipients = _recipients(db, chat)
# # # #     try:
# # # #         emit_after_commit(db, event, recipients)
# # # #     except ImportError:
# # # #         # Older app.main versions do not declare the queue yet; keep the
# # # #         # store contract intact and create the queue lazily for this process.
# # # #         from . import main

# # # #         if not hasattr(main, "event_queue"):
# # # #             main.event_queue = []
# # # #         emit_after_commit(db, event, recipients)


# # # # def _message_rows(db: Session, chat_id: str) -> list[dict[str, Any]]:
# # # #     return [value for value in rows(db, "messages") if value.get("chatId") == chat_id]


# # # # def _read_row(db: Session, chat_id: str, user_id: str) -> dict[str, Any] | None:
# # # #     return row(db, "reads", f"{chat_id}:{user_id}")


# # # # def _mark_read(db: Session, chat: dict[str, Any], user: User) -> None:
# # # #     user_id, name = _identity(db, user)
# # # #     save(
# # # #         db,
# # # #         "reads",
# # # #         {"chatId": chat["id"], "userId": user_id, "name": name, "role": _role(user), "seenAt": iso(now_utc())},
# # # #         key=f"{chat['id']}:{user_id}",
# # # #     )


# # # # def _preview_from_messages(db: Session, chat: dict[str, Any]) -> None:
# # # #     messages = _message_rows(db, str(chat["id"]))
# # # #     latest = max(messages, key=lambda value: value.get("createdAt", ""), default=None)
# # # #     if latest is None:
# # # #         chat.update(lastMessageText=None, lastMessageAt=None, lastMessageBy=None, lastMessageName=None)
# # # #     else:
# # # #         chat.update(
# # # #             lastMessageText=str(latest.get("text", ""))[:120],
# # # #             lastMessageAt=latest.get("createdAt"),
# # # #             lastMessageBy=latest.get("senderId"),
# # # #             lastMessageName=latest.get("senderName"),
# # # #         )
# # # #     save(db, "chats", chat, key=str(chat["id"]))


# # # # def _chat_view(db: Session, chat: dict[str, Any], user: User) -> dict[str, Any]:
# # # #     user_id = _uid(user)
# # # #     seen = (_read_row(db, str(chat["id"]), user_id) or {}).get("seenAt", "")
# # # #     unread = 0
# # # #     if _can_read(chat, user):
# # # #         unread = sum(
# # # #             1
# # # #             for message in _message_rows(db, str(chat["id"]))
# # # #             if message.get("senderId") != user_id and message.get("createdAt", "") > seen
# # # #         )

# # # #     direct_name: str | None = None
# # # #     direct_user_id = chat.get("directUserId")
# # # #     if direct_user_id:
# # # #         direct_user = db.get(User, direct_user_id)
# # # #         if direct_user and direct_user.employee_code:
# # # #             direct_employee = db.get(Employee, direct_user.employee_code)
# # # #             if direct_employee:
# # # #                 direct_name = f"{direct_employee.first_name} {direct_employee.last_name}"

# # # #     return {
# # # #         "id": str(chat["id"]),
# # # #         "type": chat.get("type"),
# # # #         "name": chat.get("name", ""),
# # # #         "memberIds": [str(value) for value in (chat.get("memberIds") or [])],
# # # #         "createdAt": chat.get("createdAt"),
# # # #         "announcement": bool(chat.get("announcement", False)),
# # # #         "directUserId": direct_user_id,
# # # #         "directEmployeeName": direct_name,
# # # #         "lastMessageText": chat.get("lastMessageText"),
# # # #         "lastMessageAt": chat.get("lastMessageAt"),
# # # #         "lastMessageBy": chat.get("lastMessageBy"),
# # # #         "lastMessageName": chat.get("lastMessageName"),
# # # #         "unreadCount": unread,
# # # #     }


# # # # def _clean_required(value: Any) -> str:
# # # #     return value.strip() if isinstance(value, str) else ""


# # # # def _message_view(message: dict[str, Any]) -> dict[str, Any]:
# # # #     return {
# # # #         "id": str(message["id"]),
# # # #         "senderId": message.get("senderId"),
# # # #         "senderName": message.get("senderName", ""),
# # # #         "senderRole": message.get("senderRole"),
# # # #         "text": message.get("text", ""),
# # # #         "createdAt": message.get("createdAt"),
# # # #         "replyTo": message.get("replyTo"),
# # # #         "reactions": message.get("reactions") or {},
# # # #     }


# # # # def _read_view(read: dict[str, Any]) -> dict[str, Any]:
# # # #     return {
# # # #         "userId": str(read.get("userId")),
# # # #         "name": read.get("name", ""),
# # # #         "role": read.get("role"),
# # # #         "seenAt": read.get("seenAt"),
# # # #     }


# # # # @router.get("/chats")
# # # # def list_chats(user: User = Depends(current_user), db: Session = Depends(get_db)):
# # # #     return [_chat_view(db, chat, user) for chat in rows(db, "chats") if _can_see(chat, user)]


# # # # @router.post("/chats/groups")
# # # # def create_group(data: GroupIn, user: User = Depends(admin), db: Session = Depends(get_db)):
# # # #     name = _clean_required(data.name)
# # # #     if not name:
# # # #         fail(400, "VALIDATION_ERROR", "Le nom du groupe est obligatoire.")
# # # #     lock_domain(db, "chats")
# # # #     chat = {
# # # #         "id": new_id(),
# # # #         "type": "group",
# # # #         "name": name[:MAX_GROUP_NAME_LENGTH],
# # # #         "announcement": data.announcement is True,
# # # #         "memberIds": [],
# # # #         "createdAt": iso(now_utc()),
# # # #         "directUserId": None,
# # # #         "directEmployeeName": None,
# # # #         "lastMessageText": None,
# # # #         "lastMessageAt": None,
# # # #         "lastMessageBy": None,
# # # #         "lastMessageName": None,
# # # #     }
# # # #     save(db, "chats", chat, key=chat["id"])
# # # #     db.commit()
# # # #     _emit(db, "chat.updated", chat)
# # # #     return {"id": chat["id"]}


# # # # @router.post("/chats/direct")
# # # # def open_direct(data: DirectIn, user: User = Depends(admin), db: Session = Depends(get_db)):
# # # #     code = _clean_required(data.employeeCode).upper()
# # # #     if not code:
# # # #         fail(404, "NOT_FOUND", "Salarié introuvable.")
# # # #     lock_domain(db, "chats", f"direct:{code}")
# # # #     target_employee = db.scalar(select(Employee).where(func.upper(Employee.employee_code) == code))
# # # #     if not target_employee:
# # # #         fail(404, "NOT_FOUND", "Salarié introuvable.")
# # # #     target_user = db.scalar(select(User).where(User.employee_code == target_employee.employee_code))
# # # #     if not target_user:
# # # #         fail(404, "NOT_FOUND", "Ce salarié n'a pas de compte de connexion.")
# # # #     target_id = str(target_user.id)
# # # #     existing = next(
# # # #         (chat for chat in rows(db, "chats") if chat.get("type") == "direct" and chat.get("directUserId") == target_id),
# # # #         None,
# # # #     )
# # # #     if existing:
# # # #         return {"id": existing["id"]}

# # # #     chat = {
# # # #         "id": new_id(),
# # # #         "type": "direct",
# # # #         "name": "",
# # # #         "announcement": False,
# # # #         "memberIds": [target_id],
# # # #         "createdAt": iso(now_utc()),
# # # #         "directUserId": target_id,
# # # #         "directEmployeeName": f"{target_employee.first_name} {target_employee.last_name}",
# # # #         "lastMessageText": None,
# # # #         "lastMessageAt": None,
# # # #         "lastMessageBy": None,
# # # #         "lastMessageName": None,
# # # #     }
# # # #     save(db, "chats", chat, key=chat["id"])
# # # #     db.commit()
# # # #     _emit(db, "chat.updated", chat)
# # # #     return {"id": chat["id"]}


# # # # @router.post("/chats/{chat_id}/join", status_code=204)
# # # # def join_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
# # # #     lock_domain(db, "chats", chat_id)
# # # #     chat = _chat(db, chat_id)
# # # #     if chat.get("type") != "group":
# # # #         fail(404, "NOT_FOUND", "Groupe introuvable.")
# # # #     members = [str(value) for value in (chat.get("memberIds") or [])]
# # # #     if _uid(user) not in members:
# # # #         members.append(_uid(user))
# # # #         chat["memberIds"] = members
# # # #         save(db, "chats", chat, key=chat_id)
# # # #     db.commit()
# # # #     _emit(db, "chat.updated", chat)


# # # # @router.post("/chats/{chat_id}/leave", status_code=204)
# # # # def leave_group(chat_id: str, user: User = Depends(employee), db: Session = Depends(get_db)):
# # # #     lock_domain(db, "chats", chat_id)
# # # #     chat = _chat(db, chat_id)
# # # #     if chat.get("type") != "group":
# # # #         fail(404, "NOT_FOUND", "Groupe introuvable.")
# # # #     chat["memberIds"] = [str(value) for value in (chat.get("memberIds") or []) if str(value) != _uid(user)]
# # # #     save(db, "chats", chat, key=chat_id)
# # # #     db.commit()
# # # #     _emit(db, "chat.updated", chat)


# # # # @router.get("/chats/{chat_id}/messages")
# # # # def get_messages(
# # # #     chat_id: str,
# # # #     limit: int = Query(default=300, ge=1),
# # # #     user: User = Depends(current_user),
# # # #     db: Session = Depends(get_db),
# # # # ):
# # # #     chat = _readable_chat(db, chat_id, user)
# # # #     del chat
# # # #     messages = sorted(_message_rows(db, chat_id), key=lambda value: value.get("createdAt", ""))[-limit:]
# # # #     return [_message_view(message) for message in messages]


# # # # @router.post("/chats/{chat_id}/messages", status_code=204)
# # # # def send_message(chat_id: str, data: MessageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
# # # #     lock_domain(db, "chats", chat_id)
# # # #     chat = _readable_chat(db, chat_id, user)
# # # #     if chat.get("announcement") and _role(user) != "admin":
# # # #         fail(403, "ANNOUNCEMENT_READ_ONLY", "Groupe d'annonces : seul l'administrateur peut écrire.")
# # # #     text = _clean_required(data.text)
# # # #     if not text:
# # # #         fail(400, "VALIDATION_ERROR", "Le message est vide.")
# # # #     if len(text) > MAX_MESSAGE_LENGTH:
# # # #         fail(400, "VALIDATION_ERROR", f"Message trop long ({MAX_MESSAGE_LENGTH} caractères maximum).")

# # # #     quoted = None
# # # #     if data.replyToId:
# # # #         candidate = row(db, "messages", data.replyToId)
# # # #         if candidate and candidate.get("chatId") == chat_id:
# # # #             quoted = {
# # # #                 "id": str(candidate["id"]),
# # # #                 "senderName": candidate.get("senderName", ""),
# # # #                 "text": str(candidate.get("text", ""))[:MAX_REPLY_LENGTH],
# # # #             }
# # # #     sender_id, sender_name = _identity(db, user)
# # # #     message = {
# # # #         "id": new_id(),
# # # #         "chatId": chat_id,
# # # #         "senderId": sender_id,
# # # #         "senderName": sender_name,
# # # #         "senderRole": _role(user),
# # # #         "text": text,
# # # #         "createdAt": iso(now_utc()),
# # # #         "replyTo": quoted,
# # # #         "reactions": {},
# # # #     }
# # # #     save(db, "messages", message, key=message["id"])
# # # #     chat.update(
# # # #         lastMessageText=text[:120],
# # # #         lastMessageAt=message["createdAt"],
# # # #         lastMessageBy=sender_id,
# # # #         lastMessageName=sender_name,
# # # #     )
# # # #     save(db, "chats", chat, key=chat_id)
# # # #     _mark_read(db, chat, user)
# # # #     db.commit()
# # # #     _emit(db, "message.created", chat, extra={"messageId": message["id"]})


# # # # @router.put("/chats/{chat_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
# # # # def toggle_reaction(
# # # #     chat_id: str,
# # # #     message_id: str,
# # # #     emoji: str,
# # # #     user: User = Depends(current_user),
# # # #     db: Session = Depends(get_db),
# # # # ):
# # # #     lock_domain(db, "chats", chat_id)
# # # #     chat = _readable_chat(db, chat_id, user)
# # # #     if emoji not in QUICK_REACTIONS:
# # # #         fail(400, "VALIDATION_ERROR", "Réaction non autorisée.")
# # # #     message = row(db, "messages", message_id)
# # # #     if not message or message.get("chatId") != chat_id:
# # # #         fail(404, "NOT_FOUND", "Message introuvable.")
# # # #     reactions = dict(message.get("reactions") or {})
# # # #     reacting_users = [str(value) for value in (reactions.get(emoji) or [])]
# # # #     user_id = _uid(user)
# # # #     if user_id in reacting_users:
# # # #         reacting_users = [value for value in reacting_users if value != user_id]
# # # #     else:
# # # #         reacting_users.append(user_id)
# # # #     if reacting_users:
# # # #         reactions[emoji] = reacting_users
# # # #     else:
# # # #         reactions.pop(emoji, None)
# # # #     message["reactions"] = reactions
# # # #     save(db, "messages", message, key=message_id)
# # # #     db.commit()
# # # #     _emit(db, "message.reactions", chat, extra={"messageId": message_id})


# # # # @router.delete("/chats/{chat_id}/messages/{message_id}", status_code=204)
# # # # def delete_message(chat_id: str, message_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
# # # #     lock_domain(db, "chats", chat_id)
# # # #     chat = _chat(db, chat_id)
# # # #     message = row(db, "messages", message_id)
# # # #     if not message or message.get("chatId") != chat_id:
# # # #         fail(404, "NOT_FOUND", "Message introuvable.")
# # # #     remove(db, "messages", message_id)
# # # #     _preview_from_messages(db, chat)
# # # #     db.commit()
# # # #     _emit(db, "message.deleted", chat, extra={"messageId": message_id})


# # # # @router.post("/chats/{chat_id}/clear", status_code=204)
# # # # def clear_chat(chat_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
# # # #     lock_domain(db, "chats", chat_id)
# # # #     chat = _chat(db, "chats", chat_id)
# # # #     for message in _message_rows(db, chat_id):
# # # #         remove(db, "messages", str(message["id"]))
# # # #     for read in rows(db, "reads"):
# # # #         if read.get("chatId") == chat_id:
# # # #             remove(db, "reads", f"{chat_id}:{read.get('userId')}")
# # # #     chat.update(lastMessageText=None, lastMessageAt=None, lastMessageBy=None, lastMessageName=None)
# # # #     save(db, "chats", chat, key=chat_id)
# # # #     db.commit()
# # # #     _emit(db, "chat.cleared", chat)


# # # # @router.delete("/chats/{chat_id}", status_code=204)
# # # # def delete_chat(chat_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
# # # #     lock_domain(db, "chats", chat_id)
# # # #     chat = _chat(db, "chats", chat_id)
# # # #     remove(db, "chats", chat_id)
# # # #     for message in _message_rows(db, chat_id):
# # # #         remove(db, "messages", str(message["id"]))
# # # #     for read in rows(db, "reads"):
# # # #         if read.get("chatId") == chat_id:
# # # #             remove(db, "reads", f"{chat_id}:{read.get('userId')}")
# # # #     db.commit()
# # # #     _emit(db, "chat.deleted", chat)


# # # # @router.put("/chats/{chat_id}/read", status_code=204)
# # # # def mark_seen(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
# # # #     chat = _readable_chat(db, chat_id, user)
# # # #     lock_domain(db, "reads", chat_id)
# # # #     _mark_read(db, chat, user)
# # # #     db.commit()
# # # #     _emit(db, "chat.read", chat, extra={"userId": _uid(user)})


# # # # @router.get("/chats/{chat_id}/reads")
# # # # def get_reads(chat_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
# # # #     _readable_chat(db, chat_id, user)
# # # #     return [_read_view(read) for read in rows(db, "reads") if read.get("chatId") == chat_id]
