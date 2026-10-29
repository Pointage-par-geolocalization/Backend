from __future__ import annotations

"""Clients, devis and factures backed by relational PostgreSQL tables.

The store adapter preserves the frontend's established camelCase payloads while
writing clients, documents, lines, versions and counters to normalized SQL tables.
"""

from copy import deepcopy
from datetime import datetime, date
from math import floor
import re
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from .core import admin, audit, current_user, fail, get_db, tenant_singleton
from .models import User, AppSettings
from .core import settings
from .store import emit_after_commit, iso, lock_domain, new_id, remove, row, rows, save

router = APIRouter(tags=["business"])

_OPTIONAL_CLIENT_FIELDS = ("contactName", "phone", "email", "address", "nif", "stat", "notes")
_PREFIX = {"DEVIS": "DEV", "FACTURE": "FAC"}
_QUOTE_STATUSES = ("BROUILLON", "ENVOYE", "ACCEPTE", "REFUSE")
_INVOICE_STATUSES = ("BROUILLON", "EMISE", "PAYEE", "ANNULEE")
_TRANSITIONS = {
    "DEVIS": {
        "BROUILLON": ("ENVOYE", "ACCEPTE", "REFUSE"),
        "ENVOYE": ("ACCEPTE", "REFUSE"),
        "ACCEPTE": (),
        "REFUSE": (),
    },
    "FACTURE": {
        "BROUILLON": ("EMISE",),
        "EMISE": ("PAYEE", "ANNULEE"),
        "PAYEE": (),
        "ANNULEE": (),
    },
}
_STATUS_LABELS = {
    "BROUILLON": "Brouillon",
    "ENVOYE": "Envoyé",
    "ACCEPTE": "Accepté",
    "REFUSE": "Refusé",
    "EMISE": "Émise",
    "PAYEE": "Payée",
    "ANNULEE": "Annulée",
}
TZ=ZoneInfo(settings.timezone_name)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    value = _text(value)
    return value or None


def _number(value: Any, label: str, minimum: float = 0, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        value = None
    elif isinstance(value, (int, float)):
        value = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            value = float(value)
        except ValueError:
            value = None
    else:
        value = None
    if value is None or value != value or value in (float("inf"), float("-inf")):
        fail(400, "VALIDATION_ERROR", f"Champ « {label} » invalide.")
    if value < minimum:
        fail(400, "VALIDATION_ERROR", f"Champ « {label} » : valeur minimale {minimum}.")
    if maximum is not None and value > maximum:
        fail(400, "VALIDATION_ERROR", f"Champ « {label} » : valeur maximale {maximum}.")
    return value


def _round_money(value: float) -> int:
    # All monetary inputs are non-negative; this is the equivalent of JS Math.round.
    return floor(value + 0.5)


def _day(value: Any) -> str | None:
    if value is None: return None
    if not isinstance(value,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
        fail(400,"VALIDATION_ERROR","La date doit être au format YYYY-MM-DD.")
    try: return date.fromisoformat(value).isoformat()
    except ValueError: fail(400,"VALIDATION_ERROR","La date fournie est invalide.")


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _client_fields(body: dict[str, Any], *, current: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = {**(current or {}), **body}
    name = _text(merged.get("name"))
    if not name:
        fail(400, "VALIDATION_ERROR", "Le nom du client est obligatoire.")
    result: dict[str, Any] = {"name": name}
    for key in _OPTIONAL_CLIENT_FIELDS:
        result[key] = _optional_text(merged.get(key))
    return result


def _client(db: Session, client_id: str) -> dict[str, Any]:
    value = row(db, "clients", client_id)
    if value is None:
        fail(404, "NOT_FOUND", "Client introuvable.")
    return value


def _site(db: Session, site_id: str | None) -> dict[str, Any] | None:
    return row(db, "sites", site_id) if site_id else None


def _default_tax_rate(db: Session) -> float:
    # Newer settings records are keyed as ``settings:1``; accept ``settings:app``
    # as well so imports from the browser mock remain usable.
    sql_settings=tenant_singleton(db,AppSettings,1)
    legacy = row(db, "settings", "1") or row(db, "settings", "app") or {}
    value = sql_settings.default_tax_rate if sql_settings else legacy.get("defaultTaxRate", 0)
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _content_fields(db: Session, body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if "issueDate" in body:
        result["issueDate"] = _day(body.get("issueDate")) or _today()
    elif current is None:
        result["issueDate"] = _today()
    if "dueDate" in body:
        result["dueDate"] = _day(body.get("dueDate"))
    if "subject" in body:
        result["subject"] = _text(body.get("subject"))
    if "notes" in body:
        result["notes"] = _text(body.get("notes"))
    if "siteId" in body:
        site_id = body.get("siteId")
        if site_id is None or site_id == "": result["siteId"] = None
        elif not isinstance(site_id,str) or row(db,"sites",site_id) is None: fail(404,"NOT_FOUND","Chantier introuvable.")
        else: result["siteId"] = site_id

    if "clientId" in body or current is None:
        client_id = body.get("clientId")
        client = row(db, "clients", client_id) if isinstance(client_id, str) else None
        if client is None:
            fail(400, "VALIDATION_ERROR", "Choisissez un client.")
        result["clientId"] = client["id"]
        result["client"] = {
            "name": client.get("name", ""),
            "contactName": client.get("contactName"),
            "address": client.get("address"),
            "phone": client.get("phone"),
            "email": client.get("email"),
            "nif": client.get("nif"),
            "stat": client.get("stat"),
        }

    lines = body.get("lines") if "lines" in body or current is None else current.get("lines")
    if not isinstance(lines, list) or not lines:
        fail(400, "VALIDATION_ERROR", "Ajoutez au moins une ligne.")
    recalculated: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            fail(400, "VALIDATION_ERROR", f"Ligne {index + 1} : la désignation est obligatoire.")
        description = _text(line.get("description"))
        if not description:
            fail(400, "VALIDATION_ERROR", f"Ligne {index + 1} : la désignation est obligatoire.")
        quantity = _number(line.get("quantity"), "quantité")
        unit_price = _number(line.get("unitPrice"), "prix unitaire")
        if not unit_price.is_integer(): fail(400,"VALIDATION_ERROR","Le prix unitaire doit être un montant entier en Ariary.")
        recalculated.append({
            "description": description,
            "quantity": quantity,
            "unit": _text(line.get("unit")),
            "unitPrice": unit_price,
            "amount": _round_money(quantity * unit_price),
        })
    result["lines"] = recalculated

    if "taxRate" in body:
        tax_rate = _number(body.get("taxRate"), "TVA", 0, 100)
    elif current is not None and current.get("taxRate") is not None:
        tax_rate = _number(current.get("taxRate"), "TVA", 0, 100)
    else:
        tax_rate = _number(_default_tax_rate(db), "TVA", 0, 100)
    subtotal = sum(line["amount"] for line in recalculated)
    tax_amount = _round_money(subtotal * tax_rate / 100)
    result.update({"taxRate": tax_rate, "subtotal": subtotal, "taxAmount": tax_amount, "total": subtotal + tax_amount})
    return result


def _next_number(db: Session, document_type: str) -> str:
    year = datetime.now(TZ).year
    key = f"{document_type}-{year}"
    lock_domain(db, "doc_counters", key)
    counter = row(db, "doc_counters", key) or {"id": key, "type": document_type, "year": year, "value": 0}
    value = int(counter.get("value", 0)) + 1
    counter.update({"type": document_type, "year": year, "value": value})
    save(db, "doc_counters", counter, key)
    return f"{_PREFIX[document_type]}-{year}-{value:04d}"


def _summary(db: Session, document: dict[str, Any]) -> dict[str, Any]:
    client = document.get("client") or {}
    site = _site(db, document.get("siteId"))
    return {
        "id": document.get("id"),
        "type": document.get("type"),
        "number": document.get("number"),
        "issueDate": document.get("issueDate"),
        "dueDate": document.get("dueDate"),
        "status": document.get("status"),
        "clientId": document.get("clientId"),
        "clientName": client.get("name", ""),
        "siteId": document.get("siteId"),
        "siteName": site.get("name") if site else None,
        "subject": document.get("subject", ""),
        "total": document.get("total", 0),
        "updatedAt": document.get("updatedAt"),
    }


def _full(db: Session, document: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(document)
    result.update(_summary(db, document))
    source = row(db, "documents", document.get("sourceDocumentId")) if document.get("sourceDocumentId") else None
    result["sourceDocumentNumber"] = source.get("number") if source else None
    result["invoices"] = [
        {"id": item.get("id"), "number": item.get("number"), "status": item.get("status")}
        for item in rows(db, "documents")
        if item.get("sourceDocumentId") == document.get("id")
    ]
    result["versions"] = [
        {
            "version": item.get("version"),
            "changeLabel": item.get("changeLabel"),
            "changedAt": item.get("changedAt"),
            "changedByLabel": item.get("changedByLabel"),
        }
        for item in sorted(rows(db, "document_versions"), key=lambda x: x.get("version", 0))
        if item.get("documentId") == document.get("id")
    ]
    return result


def _document(db: Session, document_id: str) -> dict[str, Any]:
    value = row(db, "documents", document_id)
    if value is None:
        fail(404, "NOT_FOUND", "Document introuvable.")
    return value


def _user_label(user: User) -> str:
    return user.email or "Administrateur"


def _snapshot(db: Session, document: dict[str, Any], label: str, user: User) -> None:
    version = int(document.get("version", 1))
    payload = {
        "id": f"{document['id']}:{version}",
        "documentId": document["id"],
        "version": version,
        "snapshot": deepcopy(document),
        "changeLabel": label,
        "changedAt": iso(),
        "changedByLabel": _user_label(user),
    }
    save(db, "document_versions", payload, payload["id"])


def _changed(db: Session) -> None:
    # The event queue is supplied by deployments that enable WebSocket delivery.
    # Keep the REST transaction usable in minimal/dev deployments without it.
    try:
        emit_after_commit(db, {"type": "document.updated"})
    except (ImportError, AttributeError):
        pass


@router.get("/clients")
def list_clients(user: User = Depends(admin), db: Session = Depends(get_db)):
    return sorted(rows(db, "clients"), key=lambda item: item.get("name", "").casefold())


@router.post("/clients", status_code=201)
def create_client(body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
    client = {"id": new_id(), **_client_fields(body)}
    save(db, "clients", client)
    audit(db, user, "CLIENT_CREATED", client["id"], f"Client « {client['name']} » créé")
    db.commit()
    return client


@router.patch("/clients/{client_id}")
def update_client(client_id: str, body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"clients",client_id)
    current = _client(db, client_id)
    current.update(_client_fields(body, current=current))
    save(db, "clients", current, client_id)
    audit(db, user, "CLIENT_UPDATED", client_id, f"Client « {current['name']} » modifié")
    db.commit()
    return current


@router.delete("/clients/{client_id}", status_code=204)
def delete_client(client_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"clients",client_id)
    client = _client(db, client_id)
    # Existing site/document links are nulled; document.client is intentionally untouched.
    for domain in ("sites", "documents"):
        for linked in rows(db, domain):
            if linked.get("clientId") == client_id:
                if domain == "sites":
                    linked["clientSnapshot"]={key:client.get(key) for key in ("name",*_OPTIONAL_CLIENT_FIELDS)}
                linked["clientId"] = None
                save(db, domain, linked, linked.get("id"))
    remove(db, "clients", client_id)
    audit(db, user, "CLIENT_DELETED", client_id, f"Client « {client.get('name', '')} » supprimé")
    db.commit()


@router.get("/documents")
def list_documents(
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    clientId: str | None = Query(default=None),
    siteId: str | None = Query(default=None),
    q: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    user: User = Depends(admin),
    db: Session = Depends(get_db),
):
    if type is not None and type not in _PREFIX: fail(400,"VALIDATION_ERROR","Type de document invalide.")
    if status is not None and status not in set(_QUOTE_STATUSES+_INVOICE_STATUSES): fail(400,"VALIDATION_ERROR","Statut de document invalide.")
    from_day=_day(from_) if from_ is not None else None
    to_day=_day(to) if to is not None else None
    needle = (q or "").strip().casefold()
    result = []
    for document in rows(db, "documents"):
        summary = _summary(db, document)
        if type and document.get("type") != type:
            continue
        if status and document.get("status") != status:
            continue
        if clientId and document.get("clientId") != clientId:
            continue
        if siteId and document.get("siteId") != siteId:
            continue
        if from_day and (document.get("issueDate") or "") < from_day:
            continue
        if to_day and (document.get("issueDate") or "") > to_day:
            continue
        if needle and not any(needle in str(value or "").casefold() for value in (
            summary["number"], summary["clientName"], summary["subject"], summary["siteName"],
        )):
            continue
        result.append(summary)
    return sorted(result, key=lambda item: (item.get("issueDate") or "", item.get("number") or ""), reverse=True)


@router.get("/documents/{document_id}")
def get_document(document_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    return _full(db, _document(db, document_id))


@router.get("/documents/{document_id}/versions/{version}")
def get_document_version(document_id: str, version: int, user: User = Depends(admin), db: Session = Depends(get_db)):
    versions = [item for item in rows(db, "document_versions") if item.get("documentId") == document_id and item.get("version") == version]
    if not versions:
        fail(404, "NOT_FOUND", "Version introuvable.")
    item = versions[0]
    return {"version": item["version"], "snapshot": _full(db, item["snapshot"])}


@router.post("/documents", status_code=201)
def create_document(body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
    document_type = body.get("type")
    if not isinstance(document_type,str) or document_type not in _PREFIX:
        fail(400, "VALIDATION_ERROR", "Type de document invalide.")
    now = iso()
    document: dict[str, Any] = {
        "id": new_id(),
        "type": document_type,
        "number": _next_number(db, document_type),
        "status": "BROUILLON",
        "dueDate": None,
        "siteId": None,
        "subject": "",
        "notes": "",
        "paidAt": None,
        "sourceDocumentId": None,
        "version": 1,
        "createdAt": now,
        "updatedAt": now,
    }
    document.update(_content_fields(db, body))
    save(db, "documents", document)
    _snapshot(db, document, "Création", user)
    audit(db, user, "DOCUMENT_CREATED", document["id"], f"{'Devis' if document_type == 'DEVIS' else 'Facture'} {document['number']} créé")
    db.commit()
    _changed(db)
    return {"id": document["id"], "number": document["number"]}


@router.patch("/documents/{document_id}")
def update_document(document_id: str, body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"documents",document_id)
    document = _document(db, document_id)
    if document.get("status") not in (("BROUILLON", "ENVOYE") if document.get("type") == "DEVIS" else ("BROUILLON",)):
        fail(409, "DOCUMENT_LOCKED", "Ce document ne peut plus être modifié.")
    document.update(_content_fields(db, body, current=document))
    document["version"] = int(document.get("version", 1)) + 1
    document["updatedAt"] = iso()
    save(db, "documents", document, document_id)
    _snapshot(db, document, "Modification du contenu", user)
    audit(db, user, "DOCUMENT_UPDATED", document_id, f"{document['number']} modifié (version {document['version']})")
    db.commit()
    _changed(db)
    return _full(db, document)


@router.post("/documents/{document_id}/status")
def set_document_status(document_id: str, body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"documents",document_id)
    document = _document(db, document_id)
    status = body.get("status")
    if status not in _TRANSITIONS.get(document.get("type"), {}).get(document.get("status"), ()):
        fail(409, "INVALID_STATUS", "Ce changement de statut n'est pas possible.")
    document.update({"status": status, "version": int(document.get("version", 1)) + 1, "updatedAt": iso()})
    if status == "PAYEE":
        document["paidAt"] = _today()
    save(db, "documents", document, document_id)
    _snapshot(db, document, f"Statut : {_STATUS_LABELS.get(status, status)}", user)
    audit(db, user, "DOCUMENT_STATUS_CHANGED", document_id, f"{document['number']} → {_STATUS_LABELS.get(status, status)}")
    db.commit()
    _changed(db)
    return _full(db, document)


@router.post("/documents/{document_id}/invoice", status_code=201)
def create_invoice_from_quote(document_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"documents",document_id)
    quote = _document(db, document_id)
    if quote.get("type") != "DEVIS":
        fail(400, "VALIDATION_ERROR", "Seul un devis peut être transformé en facture.")
    if quote.get("status") == "REFUSE":
        fail(409, "INVALID_STATUS", "Ce devis a été refusé.")
    now = iso()
    invoice = deepcopy(quote)
    invoice.update({
        "id": new_id(),
        "type": "FACTURE",
        "number": _next_number(db, "FACTURE"),
        "status": "BROUILLON",
        "issueDate": _today(),
        "dueDate": None,
        "sourceDocumentId": quote["id"],
        "paidAt": None,
        "version": 1,
        "createdAt": now,
        "updatedAt": now,
        "subject": f"Facture — {quote.get('subject', '')}" if quote.get("subject") else "",
        "notes": "",
    })
    save(db, "documents", invoice)
    _snapshot(db, invoice, f"Création depuis le devis {quote['number']}", user)
    audit(db, user, "DOCUMENT_CREATED", invoice["id"], f"Facture {invoice['number']} créée depuis le devis {quote['number']}")
    db.commit()
    _changed(db)
    return {"id": invoice["id"], "number": invoice["number"]}


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    lock_domain(db,"documents",document_id)
    document = _document(db, document_id)
    if document.get("status") != "BROUILLON":
        message = "Seul un brouillon peut être supprimé : annulez la facture." if document.get("type") == "FACTURE" else "Seul un brouillon peut être supprimé : marquez le devis comme refusé."
        fail(409, "DOCUMENT_LOCKED", message)
    remove(db, "documents", document_id)
    for version in rows(db, "document_versions"):
        if version.get("documentId") == document_id:
            remove(db, "document_versions", version.get("id", f"{document_id}:{version.get('version')}"))
    audit(db, user, "DOCUMENT_DELETED", document_id, f"Brouillon {document['number']} supprimé")
    db.commit()
    _changed(db)


# from __future__ import annotations

# """Clients, devis and factures backed by relational PostgreSQL tables.

# The store adapter preserves the frontend's established camelCase payloads while
# writing clients, documents, lines, versions and counters to normalized SQL tables.
# """

# from copy import deepcopy
# from datetime import datetime, date
# from math import floor
# import re
# from typing import Any
# from zoneinfo import ZoneInfo

# from fastapi import APIRouter, Body, Depends, Query
# from sqlalchemy.orm import Session

# from .core import admin, audit, current_user, fail, get_db
# from .models import User, AppSettings
# from .core import settings
# from .store import emit_after_commit, iso, lock_domain, new_id, remove, row, rows, save

# router = APIRouter(tags=["business"])

# _OPTIONAL_CLIENT_FIELDS = ("contactName", "phone", "email", "address", "nif", "stat", "notes")
# _PREFIX = {"DEVIS": "DEV", "FACTURE": "FAC"}
# _QUOTE_STATUSES = ("BROUILLON", "ENVOYE", "ACCEPTE", "REFUSE")
# _INVOICE_STATUSES = ("BROUILLON", "EMISE", "PAYEE", "ANNULEE")
# _TRANSITIONS = {
#     "DEVIS": {
#         "BROUILLON": ("ENVOYE", "ACCEPTE", "REFUSE"),
#         "ENVOYE": ("ACCEPTE", "REFUSE"),
#         "ACCEPTE": (),
#         "REFUSE": (),
#     },
#     "FACTURE": {
#         "BROUILLON": ("EMISE",),
#         "EMISE": ("PAYEE", "ANNULEE"),
#         "PAYEE": (),
#         "ANNULEE": (),
#     },
# }
# _STATUS_LABELS = {
#     "BROUILLON": "Brouillon",
#     "ENVOYE": "Envoyé",
#     "ACCEPTE": "Accepté",
#     "REFUSE": "Refusé",
#     "EMISE": "Émise",
#     "PAYEE": "Payée",
#     "ANNULEE": "Annulée",
# }
# TZ=ZoneInfo(settings.timezone_name)


# def _text(value: Any) -> str:
#     return value.strip() if isinstance(value, str) else ""


# def _optional_text(value: Any) -> str | None:
#     value = _text(value)
#     return value or None


# def _number(value: Any, label: str, minimum: float = 0, maximum: float | None = None) -> float:
#     if isinstance(value, bool):
#         value = None
#     elif isinstance(value, (int, float)):
#         value = float(value)
#     elif isinstance(value, str) and value.strip():
#         try:
#             value = float(value)
#         except ValueError:
#             value = None
#     else:
#         value = None
#     if value is None or value != value or value in (float("inf"), float("-inf")):
#         fail(400, "VALIDATION_ERROR", f"Champ « {label} » invalide.")
#     if value < minimum:
#         fail(400, "VALIDATION_ERROR", f"Champ « {label} » : valeur minimale {minimum}.")
#     if maximum is not None and value > maximum:
#         fail(400, "VALIDATION_ERROR", f"Champ « {label} » : valeur maximale {maximum}.")
#     return value


# def _round_money(value: float) -> int:
#     # All monetary inputs are non-negative; this is the equivalent of JS Math.round.
#     return floor(value + 0.5)


# def _day(value: Any) -> str | None:
#     if value is None: return None
#     if not isinstance(value,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
#         fail(400,"VALIDATION_ERROR","La date doit être au format YYYY-MM-DD.")
#     try: return date.fromisoformat(value).isoformat()
#     except ValueError: fail(400,"VALIDATION_ERROR","La date fournie est invalide.")


# def _today() -> str:
#     return datetime.now(TZ).date().isoformat()


# def _client_fields(body: dict[str, Any], *, current: dict[str, Any] | None = None) -> dict[str, Any]:
#     merged = {**(current or {}), **body}
#     name = _text(merged.get("name"))
#     if not name:
#         fail(400, "VALIDATION_ERROR", "Le nom du client est obligatoire.")
#     result: dict[str, Any] = {"name": name}
#     for key in _OPTIONAL_CLIENT_FIELDS:
#         result[key] = _optional_text(merged.get(key))
#     return result


# def _client(db: Session, client_id: str) -> dict[str, Any]:
#     value = row(db, "clients", client_id)
#     if value is None:
#         fail(404, "NOT_FOUND", "Client introuvable.")
#     return value


# def _site(db: Session, site_id: str | None) -> dict[str, Any] | None:
#     return row(db, "sites", site_id) if site_id else None


# def _default_tax_rate(db: Session) -> float:
#     # Newer settings records are keyed as ``settings:1``; accept ``settings:app``
#     # as well so imports from the browser mock remain usable.
#     sql_settings=db.get(AppSettings,1)
#     legacy = row(db, "settings", "1") or row(db, "settings", "app") or {}
#     value = sql_settings.default_tax_rate if sql_settings else legacy.get("defaultTaxRate", 0)
#     try:
#         return float(value) if value is not None else 0.0
#     except (TypeError, ValueError):
#         return 0.0


# def _content_fields(db: Session, body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
#     result: dict[str, Any] = {}
#     if "issueDate" in body:
#         result["issueDate"] = _day(body.get("issueDate")) or _today()
#     elif current is None:
#         result["issueDate"] = _today()
#     if "dueDate" in body:
#         result["dueDate"] = _day(body.get("dueDate"))
#     if "subject" in body:
#         result["subject"] = _text(body.get("subject"))
#     if "notes" in body:
#         result["notes"] = _text(body.get("notes"))
#     if "siteId" in body:
#         site_id = body.get("siteId")
#         if site_id is None or site_id == "": result["siteId"] = None
#         elif not isinstance(site_id,str) or row(db,"sites",site_id) is None: fail(404,"NOT_FOUND","Chantier introuvable.")
#         else: result["siteId"] = site_id

#     if "clientId" in body or current is None:
#         client_id = body.get("clientId")
#         client = row(db, "clients", client_id) if isinstance(client_id, str) else None
#         if client is None:
#             fail(400, "VALIDATION_ERROR", "Choisissez un client.")
#         result["clientId"] = client["id"]
#         result["client"] = {
#             "name": client.get("name", ""),
#             "contactName": client.get("contactName"),
#             "address": client.get("address"),
#             "phone": client.get("phone"),
#             "email": client.get("email"),
#             "nif": client.get("nif"),
#             "stat": client.get("stat"),
#         }

#     lines = body.get("lines") if "lines" in body or current is None else current.get("lines")
#     if not isinstance(lines, list) or not lines:
#         fail(400, "VALIDATION_ERROR", "Ajoutez au moins une ligne.")
#     recalculated: list[dict[str, Any]] = []
#     for index, line in enumerate(lines):
#         if not isinstance(line, dict):
#             fail(400, "VALIDATION_ERROR", f"Ligne {index + 1} : la désignation est obligatoire.")
#         description = _text(line.get("description"))
#         if not description:
#             fail(400, "VALIDATION_ERROR", f"Ligne {index + 1} : la désignation est obligatoire.")
#         quantity = _number(line.get("quantity"), "quantité")
#         unit_price = _number(line.get("unitPrice"), "prix unitaire")
#         if not unit_price.is_integer(): fail(400,"VALIDATION_ERROR","Le prix unitaire doit être un montant entier en Ariary.")
#         recalculated.append({
#             "description": description,
#             "quantity": quantity,
#             "unit": _text(line.get("unit")),
#             "unitPrice": unit_price,
#             "amount": _round_money(quantity * unit_price),
#         })
#     result["lines"] = recalculated

#     if "taxRate" in body:
#         tax_rate = _number(body.get("taxRate"), "TVA", 0, 100)
#     elif current is not None and current.get("taxRate") is not None:
#         tax_rate = _number(current.get("taxRate"), "TVA", 0, 100)
#     else:
#         tax_rate = _number(_default_tax_rate(db), "TVA", 0, 100)
#     subtotal = sum(line["amount"] for line in recalculated)
#     tax_amount = _round_money(subtotal * tax_rate / 100)
#     result.update({"taxRate": tax_rate, "subtotal": subtotal, "taxAmount": tax_amount, "total": subtotal + tax_amount})
#     return result


# def _next_number(db: Session, document_type: str) -> str:
#     year = datetime.now(TZ).year
#     key = f"{document_type}-{year}"
#     lock_domain(db, "doc_counters", key)
#     counter = row(db, "doc_counters", key) or {"id": key, "type": document_type, "year": year, "value": 0}
#     value = int(counter.get("value", 0)) + 1
#     counter.update({"type": document_type, "year": year, "value": value})
#     save(db, "doc_counters", counter, key)
#     return f"{_PREFIX[document_type]}-{year}-{value:04d}"


# def _summary(db: Session, document: dict[str, Any]) -> dict[str, Any]:
#     client = document.get("client") or {}
#     site = _site(db, document.get("siteId"))
#     return {
#         "id": document.get("id"),
#         "type": document.get("type"),
#         "number": document.get("number"),
#         "issueDate": document.get("issueDate"),
#         "dueDate": document.get("dueDate"),
#         "status": document.get("status"),
#         "clientId": document.get("clientId"),
#         "clientName": client.get("name", ""),
#         "siteId": document.get("siteId"),
#         "siteName": site.get("name") if site else None,
#         "subject": document.get("subject", ""),
#         "total": document.get("total", 0),
#         "updatedAt": document.get("updatedAt"),
#     }


# def _full(db: Session, document: dict[str, Any]) -> dict[str, Any]:
#     result = deepcopy(document)
#     result.update(_summary(db, document))
#     source = row(db, "documents", document.get("sourceDocumentId")) if document.get("sourceDocumentId") else None
#     result["sourceDocumentNumber"] = source.get("number") if source else None
#     result["invoices"] = [
#         {"id": item.get("id"), "number": item.get("number"), "status": item.get("status")}
#         for item in rows(db, "documents")
#         if item.get("sourceDocumentId") == document.get("id")
#     ]
#     result["versions"] = [
#         {
#             "version": item.get("version"),
#             "changeLabel": item.get("changeLabel"),
#             "changedAt": item.get("changedAt"),
#             "changedByLabel": item.get("changedByLabel"),
#         }
#         for item in sorted(rows(db, "document_versions"), key=lambda x: x.get("version", 0))
#         if item.get("documentId") == document.get("id")
#     ]
#     return result


# def _document(db: Session, document_id: str) -> dict[str, Any]:
#     value = row(db, "documents", document_id)
#     if value is None:
#         fail(404, "NOT_FOUND", "Document introuvable.")
#     return value


# def _user_label(user: User) -> str:
#     return user.email or "Administrateur"


# def _snapshot(db: Session, document: dict[str, Any], label: str, user: User) -> None:
#     version = int(document.get("version", 1))
#     payload = {
#         "id": f"{document['id']}:{version}",
#         "documentId": document["id"],
#         "version": version,
#         "snapshot": deepcopy(document),
#         "changeLabel": label,
#         "changedAt": iso(),
#         "changedByLabel": _user_label(user),
#     }
#     save(db, "document_versions", payload, payload["id"])


# def _changed(db: Session) -> None:
#     # The event queue is supplied by deployments that enable WebSocket delivery.
#     # Keep the REST transaction usable in minimal/dev deployments without it.
#     try:
#         emit_after_commit(db, {"type": "document.updated"})
#     except (ImportError, AttributeError):
#         pass


# @router.get("/clients")
# def list_clients(user: User = Depends(admin), db: Session = Depends(get_db)):
#     return sorted(rows(db, "clients"), key=lambda item: item.get("name", "").casefold())


# @router.post("/clients", status_code=201)
# def create_client(body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
#     client = {"id": new_id(), **_client_fields(body)}
#     save(db, "clients", client)
#     audit(db, user, "CLIENT_CREATED", client["id"], f"Client « {client['name']} » créé")
#     db.commit()
#     return client


# @router.patch("/clients/{client_id}")
# def update_client(client_id: str, body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
#     lock_domain(db,"clients",client_id)
#     current = _client(db, client_id)
#     current.update(_client_fields(body, current=current))
#     save(db, "clients", current, client_id)
#     audit(db, user, "CLIENT_UPDATED", client_id, f"Client « {current['name']} » modifié")
#     db.commit()
#     return current


# @router.delete("/clients/{client_id}", status_code=204)
# def delete_client(client_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
#     lock_domain(db,"clients",client_id)
#     client = _client(db, client_id)
#     # Existing site/document links are nulled; document.client is intentionally untouched.
#     for domain in ("sites", "documents"):
#         for linked in rows(db, domain):
#             if linked.get("clientId") == client_id:
#                 if domain == "sites":
#                     linked["clientSnapshot"]={key:client.get(key) for key in ("name",*_OPTIONAL_CLIENT_FIELDS)}
#                 linked["clientId"] = None
#                 save(db, domain, linked, linked.get("id"))
#     remove(db, "clients", client_id)
#     audit(db, user, "CLIENT_DELETED", client_id, f"Client « {client.get('name', '')} » supprimé")
#     db.commit()


# @router.get("/documents")
# def list_documents(
#     type: str | None = Query(default=None),
#     status: str | None = Query(default=None),
#     clientId: str | None = Query(default=None),
#     siteId: str | None = Query(default=None),
#     q: str | None = Query(default=None),
#     from_: str | None = Query(default=None, alias="from"),
#     to: str | None = Query(default=None),
#     user: User = Depends(admin),
#     db: Session = Depends(get_db),
# ):
#     if type is not None and type not in _PREFIX: fail(400,"VALIDATION_ERROR","Type de document invalide.")
#     if status is not None and status not in set(_QUOTE_STATUSES+_INVOICE_STATUSES): fail(400,"VALIDATION_ERROR","Statut de document invalide.")
#     from_day=_day(from_) if from_ is not None else None
#     to_day=_day(to) if to is not None else None
#     needle = (q or "").strip().casefold()
#     result = []
#     for document in rows(db, "documents"):
#         summary = _summary(db, document)
#         if type and document.get("type") != type:
#             continue
#         if status and document.get("status") != status:
#             continue
#         if clientId and document.get("clientId") != clientId:
#             continue
#         if siteId and document.get("siteId") != siteId:
#             continue
#         if from_day and (document.get("issueDate") or "") < from_day:
#             continue
#         if to_day and (document.get("issueDate") or "") > to_day:
#             continue
#         if needle and not any(needle in str(value or "").casefold() for value in (
#             summary["number"], summary["clientName"], summary["subject"], summary["siteName"],
#         )):
#             continue
#         result.append(summary)
#     return sorted(result, key=lambda item: (item.get("issueDate") or "", item.get("number") or ""), reverse=True)


# @router.get("/documents/{document_id}")
# def get_document(document_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
#     return _full(db, _document(db, document_id))


# @router.get("/documents/{document_id}/versions/{version}")
# def get_document_version(document_id: str, version: int, user: User = Depends(admin), db: Session = Depends(get_db)):
#     versions = [item for item in rows(db, "document_versions") if item.get("documentId") == document_id and item.get("version") == version]
#     if not versions:
#         fail(404, "NOT_FOUND", "Version introuvable.")
#     item = versions[0]
#     return {"version": item["version"], "snapshot": _full(db, item["snapshot"])}


# @router.post("/documents", status_code=201)
# def create_document(body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
#     document_type = body.get("type")
#     if not isinstance(document_type,str) or document_type not in _PREFIX:
#         fail(400, "VALIDATION_ERROR", "Type de document invalide.")
#     now = iso()
#     document: dict[str, Any] = {
#         "id": new_id(),
#         "type": document_type,
#         "number": _next_number(db, document_type),
#         "status": "BROUILLON",
#         "dueDate": None,
#         "siteId": None,
#         "subject": "",
#         "notes": "",
#         "paidAt": None,
#         "sourceDocumentId": None,
#         "version": 1,
#         "createdAt": now,
#         "updatedAt": now,
#     }
#     document.update(_content_fields(db, body))
#     save(db, "documents", document)
#     _snapshot(db, document, "Création", user)
#     audit(db, user, "DOCUMENT_CREATED", document["id"], f"{'Devis' if document_type == 'DEVIS' else 'Facture'} {document['number']} créé")
#     db.commit()
#     _changed(db)
#     return {"id": document["id"], "number": document["number"]}


# @router.patch("/documents/{document_id}")
# def update_document(document_id: str, body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
#     lock_domain(db,"documents",document_id)
#     document = _document(db, document_id)
#     if document.get("status") not in (("BROUILLON", "ENVOYE") if document.get("type") == "DEVIS" else ("BROUILLON",)):
#         fail(409, "DOCUMENT_LOCKED", "Ce document ne peut plus être modifié.")
#     document.update(_content_fields(db, body, current=document))
#     document["version"] = int(document.get("version", 1)) + 1
#     document["updatedAt"] = iso()
#     save(db, "documents", document, document_id)
#     _snapshot(db, document, "Modification du contenu", user)
#     audit(db, user, "DOCUMENT_UPDATED", document_id, f"{document['number']} modifié (version {document['version']})")
#     db.commit()
#     _changed(db)
#     return _full(db, document)


# @router.post("/documents/{document_id}/status")
# def set_document_status(document_id: str, body: dict[str, Any] = Body(...), user: User = Depends(admin), db: Session = Depends(get_db)):
#     lock_domain(db,"documents",document_id)
#     document = _document(db, document_id)
#     status = body.get("status")
#     if status not in _TRANSITIONS.get(document.get("type"), {}).get(document.get("status"), ()):
#         fail(409, "INVALID_STATUS", "Ce changement de statut n'est pas possible.")
#     document.update({"status": status, "version": int(document.get("version", 1)) + 1, "updatedAt": iso()})
#     if status == "PAYEE":
#         document["paidAt"] = _today()
#     save(db, "documents", document, document_id)
#     _snapshot(db, document, f"Statut : {_STATUS_LABELS.get(status, status)}", user)
#     audit(db, user, "DOCUMENT_STATUS_CHANGED", document_id, f"{document['number']} → {_STATUS_LABELS.get(status, status)}")
#     db.commit()
#     _changed(db)
#     return _full(db, document)


# @router.post("/documents/{document_id}/invoice", status_code=201)
# def create_invoice_from_quote(document_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
#     lock_domain(db,"documents",document_id)
#     quote = _document(db, document_id)
#     if quote.get("type") != "DEVIS":
#         fail(400, "VALIDATION_ERROR", "Seul un devis peut être transformé en facture.")
#     if quote.get("status") == "REFUSE":
#         fail(409, "INVALID_STATUS", "Ce devis a été refusé.")
#     now = iso()
#     invoice = deepcopy(quote)
#     invoice.update({
#         "id": new_id(),
#         "type": "FACTURE",
#         "number": _next_number(db, "FACTURE"),
#         "status": "BROUILLON",
#         "issueDate": _today(),
#         "dueDate": None,
#         "sourceDocumentId": quote["id"],
#         "paidAt": None,
#         "version": 1,
#         "createdAt": now,
#         "updatedAt": now,
#         "subject": f"Facture — {quote.get('subject', '')}" if quote.get("subject") else "",
#         "notes": "",
#     })
#     save(db, "documents", invoice)
#     _snapshot(db, invoice, f"Création depuis le devis {quote['number']}", user)
#     audit(db, user, "DOCUMENT_CREATED", invoice["id"], f"Facture {invoice['number']} créée depuis le devis {quote['number']}")
#     db.commit()
#     _changed(db)
#     return {"id": invoice["id"], "number": invoice["number"]}


# @router.delete("/documents/{document_id}", status_code=204)
# def delete_document(document_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
#     lock_domain(db,"documents",document_id)
#     document = _document(db, document_id)
#     if document.get("status") != "BROUILLON":
#         message = "Seul un brouillon peut être supprimé : annulez la facture." if document.get("type") == "FACTURE" else "Seul un brouillon peut être supprimé : marquez le devis comme refusé."
#         fail(409, "DOCUMENT_LOCKED", message)
#     remove(db, "documents", document_id)
#     for version in rows(db, "document_versions"):
#         if version.get("documentId") == document_id:
#             remove(db, "document_versions", version.get("id", f"{document_id}:{version.get('version')}"))
#     audit(db, user, "DOCUMENT_DELETED", document_id, f"Brouillon {document['number']} supprimé")
#     db.commit()
#     _changed(db)
