#!/usr/bin/env python3
"""Create the first administrator after `alembic upgrade head` has been applied."""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

# When invoked as `python scripts/create_admin.py`, Python puts `scripts/`
# rather than the backend project root at sys.path[0] (notably on Windows).
BACKEND_DIR = str(Path(__file__).resolve().parents[1])
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import func, select
from pydantic import TypeAdapter, EmailStr, ValidationError

from app.core import SessionLocal, hash_password, set_tenant_context
from app.models import AuditLog, Role, User
from uuid import uuid4


def main() -> None:
    try:
        email = TypeAdapter(EmailStr).validate_python(input("Email admin : ").strip()).lower()
    except ValidationError as exc:
        raise SystemExit(f"Adresse email invalide : {exc.errors()[0]['msg']}")
    password = getpass.getpass("Mot de passe (6 caractères minimum) : ")
    confirm = getpass.getpass("Confirmer le mot de passe : ")
    if len(password) < 6:
        raise SystemExit("Le mot de passe doit contenir au moins 6 caractères.")
    if password != confirm:
        raise SystemExit("Les mots de passe ne correspondent pas.")

    db = SessionLocal()
    try:
        set_tenant_context(db, None, auth_lookup=True)
        existing = db.scalar(select(func.count()).select_from(User).where(User.role == Role.admin)) or 0
        if existing:
            raise SystemExit("Un administrateur existe déjà. Ce script ne crée que le premier admin.")
        if db.scalar(select(User.id).where(func.lower(User.email) == email)):
            raise SystemExit("Cette adresse email est déjà utilisée.")
        admin_id = uuid4()
        set_tenant_context(db, admin_id)
        user = User(id=admin_id, tenant_id=admin_id, role=Role.admin, email=email, password_hash=hash_password(password))
        db.add(user)
        db.flush()
        db.add(AuditLog(user_id=user.id, user_label="Système", action="ADMIN_CREATED", target_id=str(user.id), summary=f"Premier compte administrateur {email} créé par le script de bootstrap"))
        db.commit()
        print(f"Compte administrateur créé : {email}")
    except SystemExit:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()


# #!/usr/bin/env python3
# """Create the first administrator after `alembic upgrade head` has been applied."""
# from __future__ import annotations

# import getpass
# import sys
# from pathlib import Path

# # When invoked as `python scripts/create_admin.py`, Python puts `scripts/`
# # rather than the backend project root at sys.path[0] (notably on Windows).
# BACKEND_DIR = str(Path(__file__).resolve().parents[1])
# if BACKEND_DIR not in sys.path:
#     sys.path.insert(0, BACKEND_DIR)

# from sqlalchemy import func, select
# from pydantic import TypeAdapter, EmailStr, ValidationError

# from app.core import SessionLocal, hash_password
# from app.models import AuditLog, Role, User


# def main() -> None:
#     try:
#         email = TypeAdapter(EmailStr).validate_python(input("Email admin : ").strip()).lower()
#     except ValidationError as exc:
#         raise SystemExit(f"Adresse email invalide : {exc.errors()[0]['msg']}")
#     password = getpass.getpass("Mot de passe (6 caractères minimum) : ")
#     confirm = getpass.getpass("Confirmer le mot de passe : ")
#     if len(password) < 6:
#         raise SystemExit("Le mot de passe doit contenir au moins 6 caractères.")
#     if password != confirm:
#         raise SystemExit("Les mots de passe ne correspondent pas.")

#     db = SessionLocal()
#     try:
#         existing = db.scalar(select(func.count()).select_from(User).where(User.role == Role.admin)) or 0
#         if existing:
#             raise SystemExit("Un administrateur existe déjà. Ce script ne crée que le premier admin.")
#         if db.scalar(select(User.id).where(func.lower(User.email) == email)):
#             raise SystemExit("Cette adresse email est déjà utilisée.")
#         user = User(role=Role.admin, email=email, password_hash=hash_password(password))
#         db.add(user)
#         db.flush()
#         db.add(AuditLog(user_id=user.id, user_label="Système", action="ADMIN_CREATED", target_id=str(user.id), summary=f"Premier compte administrateur {email} créé par le script de bootstrap"))
#         db.commit()
#         print(f"Compte administrateur créé : {email}")
#     except SystemExit:
#         db.rollback()
#         raise
#     except Exception:
#         db.rollback()
#         raise
#     finally:
#         db.close()


# if __name__ == "__main__":
#     main()
