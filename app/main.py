from __future__ import annotations
import asyncio, os, logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID
import jwt
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse
from .core import settings, api_error_handler, engine, SessionLocal, set_tenant_context
from .models import Base, User, Role
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
# Imports avant create_all: ils enregistrent tous les modèles dans Base.metadata.
from .store import hub
from .auth import router as auth_router
from .api import router as api_router, auto_close_attendance_tick
from .equipment_api import router as equipment_router
from .chat_api import router as chat_router
from .leaves_api import router as leaves_router
from .sites_api import router as sites_router
from .business_api import router as business_router
from .presence_api import router as presence_router
from .presence_api import presence_tick
from .admin_api import router as admin_router
from .feature_api import router as feature_router

event_queue: list[tuple[dict, set[str] | None]] = []
logger=logging.getLogger("pointage")

async def _dispatch_events():
    while True:
        pending = event_queue[:]
        del event_queue[:]
        for event, recipients in pending:
            await hub.publish(event, recipients)
        await asyncio.sleep(0.05)

def _presence_tick_once():
    db=SessionLocal()
    try:
        # Discover administrator tenants in the narrow auth lookup context, then run
        # every scheduler query under one tenant at a time.
        set_tenant_context(db, None, auth_lookup=True)
        tenants=list(db.scalars(select(User.id).where(User.role==Role.admin, User.active.is_(True))).all())
        db.rollback()
        for tenant_id in tenants:
            set_tenant_context(db, tenant_id)
            try:
                changed=presence_tick(db)
                changed=auto_close_attendance_tick(db) or changed
                if changed: db.commit()
                else: db.rollback()
            except Exception:
                logger.exception("Échec du contrôle de présence pour le tenant %s", tenant_id)
                db.rollback()
    except Exception:
        logger.exception("Échec d'une itération de contrôle de présence")
        db.rollback()
    finally: db.close()

async def _presence_loop():
    while True:
        await asyncio.to_thread(_presence_tick_once)
        await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("AUTO_CREATE_SCHEMA", "false").lower() == "true":
        Base.metadata.create_all(engine)
    if settings.environment.lower() == "production":
        if len(settings.jwt_access_secret.encode()) < 32 or settings.jwt_access_secret == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET" or settings.jwt_access_secret.lower().startswith(("replace-with", "change_me", "changeme")):
            raise RuntimeError("JWT_ACCESS_SECRET doit contenir au moins 32 octets aléatoires en production")
        if len(settings.invite_code) < 16 or settings.invite_code == "CHANGE_ME" or settings.invite_code.lower().startswith(("replace-with", "change_me", "changeme")):
            raise RuntimeError("INVITE_CODE doit contenir au moins 16 caractères en production")
        if "change_me" in settings.database_url.lower() or "localhost" in settings.database_url.lower() or "127.0.0.1" in settings.database_url:
            raise RuntimeError("DATABASE_URL doit désigner un serveur de base distant, non une valeur locale/exemple, en production")
        if not settings.cookie_secure:
            raise RuntimeError("COOKIE_SECURE=true est obligatoire en production")
        if settings.cookie_samesite.lower() not in {"lax","strict","none"}:
            raise RuntimeError("COOKIE_SAMESITE doit être Lax, Strict ou None")
        if settings.cookie_samesite.lower()=="none" and not settings.cookie_secure:
            raise RuntimeError("SameSite=None exige Secure")
        if not origins or "*" in origins:
            raise RuntimeError("FRONTEND_ORIGINS doit contenir une ou plusieurs origines explicites en production")
        if any(origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1") for origin in origins):
            raise RuntimeError("FRONTEND_ORIGINS contient encore une origine de développement local")
        if os.getenv("AUTO_CREATE_SCHEMA","false").lower()=="true":
            raise RuntimeError("AUTO_CREATE_SCHEMA doit rester désactivé en production; utiliser alembic upgrade head")
    task=asyncio.create_task(_dispatch_events())
    presence_task=asyncio.create_task(_presence_loop())
    try:
        yield
    finally:
        task.cancel()
        presence_task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        try: await presence_task
        except asyncio.CancelledError: pass

app=FastAPI(title="Pointage Fortico API",version="2.0.0",description="API complète du frontend Pointage Fortico — FastAPI, PostgreSQL et WebSocket",lifespan=lifespan)
origins=[x.strip() for x in settings.frontend_origins.split(',') if x.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=True,allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],allow_headers=["Authorization","Content-Type","Accept"])
app.add_exception_handler(HTTPException,api_error_handler)
@app.exception_handler(IntegrityError)
async def integrity_error(request:Request,exc:IntegrityError):
    logger.info("Conflit de contrainte de données sur %s",request.url.path)
    return JSONResponse(status_code=409,content={"error":{"code":"CONFLICT","message":"Cette modification entre en conflit avec des données déjà enregistrées.","details":None}})

@app.exception_handler(Exception)
async def unexpected_error(request:Request,exc:Exception):
    logger.exception("Erreur interne sur %s",request.url.path)
    return JSONResponse(status_code=500,content={"error":{"code":"INTERNAL_ERROR","message":"Une erreur interne est survenue.","details":None}})

@app.exception_handler(RequestValidationError)
async def validation_error(request:Request,exc:RequestValidationError):
    fields=[]
    for item in exc.errors():
        fields.append({"field":".".join(str(x) for x in item.get("loc",())[1:]),"message":item.get("msg","Donnée invalide")})
    return JSONResponse(status_code=400,content={"error":{"code":"VALIDATION_ERROR","message":"Les données envoyées sont invalides.","details":{"fields":fields}}})

for route in (auth_router,api_router,equipment_router,chat_router,leaves_router,sites_router,business_router,presence_router,admin_router,feature_router):
    app.include_router(route,prefix="/api/v1")

@app.get('/health')
def health(): return {"status":"ok"}

@app.get('/ready')
def ready():
    from sqlalchemy import text
    with engine.connect() as connection: connection.execute(text("SELECT 1"))
    return {"status":"ready","database":"ok"}

async def websocket_endpoint(ws: WebSocket):
    token=ws.query_params.get("token","")
    try:
        claims=jwt.decode(token,settings.jwt_access_secret,algorithms=["HS256"])
        uid=UUID(claims["sub"])
        db=SessionLocal()
        claimed_tenant=UUID(claims["tenantId"]) if claims.get("tenantId") else None
        set_tenant_context(db, claimed_tenant, auth_lookup=claimed_tenant is None)
        user=db.get(User,uid)
        if not user or not user.active or claims.get("ver")!=user.token_version:
            db.close(); await ws.close(code=4401); return
        if claimed_tenant is None:
            set_tenant_context(db,user.tenant_id)
        elif claimed_tenant != user.tenant_id:
            db.close(); await ws.close(code=4401); return
        if user.role.value=="employee":
            from .models import Employee
            employee=db.get(Employee,user.employee_code)
            if not employee or not employee.active:
                db.close(); await ws.close(code=4401); return
        expires=float(claims["exp"])
        await hub.connect(ws,str(user.id),user.role.value,user.employee_code)
        db.close()
    except Exception:
        try: await ws.close(code=4401)
        except Exception: pass
        return
    uid=str(uid)
    try:
        while True:
            remaining=expires-datetime.now(timezone.utc).timestamp()
            if remaining<=0:
                await ws.close(code=4401); break
            try: await asyncio.wait_for(ws.receive_text(),timeout=min(30,remaining))
            except asyncio.TimeoutError: continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally: hub.disconnect(uid)

app.websocket('/api/v1/ws')(websocket_endpoint)
app.websocket('/ws')(websocket_endpoint)


# from __future__ import annotations
# import asyncio, os, logging
# from contextlib import asynccontextmanager
# from datetime import datetime, timezone
# from uuid import UUID
# import jwt
# from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.exceptions import HTTPException, RequestValidationError
# from fastapi.responses import JSONResponse
# from .core import settings, api_error_handler, engine, SessionLocal
# from .models import Base, User
# from sqlalchemy.exc import IntegrityError
# # Imports avant create_all: ils enregistrent tous les modèles dans Base.metadata.
# from .store import hub
# from .auth import router as auth_router
# from .api import router as api_router, auto_close_attendance_tick
# from .equipment_api import router as equipment_router
# from .chat_api import router as chat_router
# from .leaves_api import router as leaves_router
# from .sites_api import router as sites_router
# from .business_api import router as business_router
# from .presence_api import router as presence_router
# from .presence_api import presence_tick
# from .admin_api import router as admin_router
# from .feature_api import router as feature_router

# event_queue: list[tuple[dict, set[str] | None]] = []
# logger=logging.getLogger("pointage")

# async def _dispatch_events():
#     while True:
#         pending = event_queue[:]
#         del event_queue[:]
#         for event, recipients in pending:
#             await hub.publish(event, recipients)
#         await asyncio.sleep(0.05)

# def _presence_tick_once():
#     db=SessionLocal()
#     try:
#         changed = presence_tick(db)
#         changed = auto_close_attendance_tick(db) or changed
#         if changed: db.commit()
#         else: db.rollback()
#     except Exception:
#         logger.exception("Échec d'une itération de contrôle de présence")
#         db.rollback()
#     finally: db.close()

# async def _presence_loop():
#     while True:
#         await asyncio.to_thread(_presence_tick_once)
#         await asyncio.sleep(10)

# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     if os.getenv("AUTO_CREATE_SCHEMA", "false").lower() == "true":
#         Base.metadata.create_all(engine)
#     if settings.environment.lower() == "production":
#         if len(settings.jwt_access_secret.encode()) < 32 or settings.jwt_access_secret == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET" or settings.jwt_access_secret.lower().startswith(("replace-with", "change_me", "changeme")):
#             raise RuntimeError("JWT_ACCESS_SECRET doit contenir au moins 32 octets aléatoires en production")
#         if len(settings.invite_code) < 16 or settings.invite_code == "CHANGE_ME" or settings.invite_code.lower().startswith(("replace-with", "change_me", "changeme")):
#             raise RuntimeError("INVITE_CODE doit contenir au moins 16 caractères en production")
#         if "change_me" in settings.database_url.lower() or "localhost" in settings.database_url.lower() or "127.0.0.1" in settings.database_url:
#             raise RuntimeError("DATABASE_URL doit désigner un serveur de base distant, non une valeur locale/exemple, en production")
#         if not settings.cookie_secure:
#             raise RuntimeError("COOKIE_SECURE=true est obligatoire en production")
#         if settings.cookie_samesite.lower() not in {"lax","strict","none"}:
#             raise RuntimeError("COOKIE_SAMESITE doit être Lax, Strict ou None")
#         if settings.cookie_samesite.lower()=="none" and not settings.cookie_secure:
#             raise RuntimeError("SameSite=None exige Secure")
#         if not origins or "*" in origins:
#             raise RuntimeError("FRONTEND_ORIGINS doit contenir une ou plusieurs origines explicites en production")
#         if any(origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1") for origin in origins):
#             raise RuntimeError("FRONTEND_ORIGINS contient encore une origine de développement local")
#         if os.getenv("AUTO_CREATE_SCHEMA","false").lower()=="true":
#             raise RuntimeError("AUTO_CREATE_SCHEMA doit rester désactivé en production; utiliser alembic upgrade head")
#     task=asyncio.create_task(_dispatch_events())
#     presence_task=asyncio.create_task(_presence_loop())
#     try:
#         yield
#     finally:
#         task.cancel()
#         presence_task.cancel()
#         try: await task
#         except asyncio.CancelledError: pass
#         try: await presence_task
#         except asyncio.CancelledError: pass

# app=FastAPI(title="Pointage Fortico API",version="2.0.0",description="API complète du frontend Pointage Fortico — FastAPI, PostgreSQL et WebSocket",lifespan=lifespan)
# origins=[x.strip() for x in settings.frontend_origins.split(',') if x.strip()]
# app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=True,allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],allow_headers=["Authorization","Content-Type","Accept"])
# app.add_exception_handler(HTTPException,api_error_handler)
# @app.exception_handler(IntegrityError)
# async def integrity_error(request:Request,exc:IntegrityError):
#     logger.info("Conflit de contrainte de données sur %s",request.url.path)
#     return JSONResponse(status_code=409,content={"error":{"code":"CONFLICT","message":"Cette modification entre en conflit avec des données déjà enregistrées.","details":None}})

# @app.exception_handler(Exception)
# async def unexpected_error(request:Request,exc:Exception):
#     logger.exception("Erreur interne sur %s",request.url.path)
#     return JSONResponse(status_code=500,content={"error":{"code":"INTERNAL_ERROR","message":"Une erreur interne est survenue.","details":None}})

# @app.exception_handler(RequestValidationError)
# async def validation_error(request:Request,exc:RequestValidationError):
#     fields=[]
#     for item in exc.errors():
#         fields.append({"field":".".join(str(x) for x in item.get("loc",())[1:]),"message":item.get("msg","Donnée invalide")})
#     return JSONResponse(status_code=400,content={"error":{"code":"VALIDATION_ERROR","message":"Les données envoyées sont invalides.","details":{"fields":fields}}})

# for route in (auth_router,api_router,equipment_router,chat_router,leaves_router,sites_router,business_router,presence_router,admin_router,feature_router):
#     app.include_router(route,prefix="/api/v1")

# @app.get('/health')
# def health(): return {"status":"ok"}

# @app.get('/ready')
# def ready():
#     from sqlalchemy import text
#     with engine.connect() as connection: connection.execute(text("SELECT 1"))
#     return {"status":"ready","database":"ok"}

# async def websocket_endpoint(ws: WebSocket):
#     token=ws.query_params.get("token","")
#     try:
#         claims=jwt.decode(token,settings.jwt_access_secret,algorithms=["HS256"])
#         uid=UUID(claims["sub"])
#         db=SessionLocal()
#         user=db.get(User,uid)
#         if not user or not user.active or claims.get("ver")!=user.token_version:
#             db.close(); await ws.close(code=4401); return
#         if user.role.value=="employee":
#             from .models import Employee
#             employee=db.get(Employee,user.employee_code)
#             if not employee or not employee.active:
#                 db.close(); await ws.close(code=4401); return
#         expires=float(claims["exp"])
#         await hub.connect(ws,str(user.id),user.role.value,user.employee_code)
#         db.close()
#     except Exception:
#         try: await ws.close(code=4401)
#         except Exception: pass
#         return
#     uid=str(uid)
#     try:
#         while True:
#             remaining=expires-datetime.now(timezone.utc).timestamp()
#             if remaining<=0:
#                 await ws.close(code=4401); break
#             try: await asyncio.wait_for(ws.receive_text(),timeout=min(30,remaining))
#             except asyncio.TimeoutError: continue
#     except WebSocketDisconnect:
#         pass
#     except Exception:
#         pass
#     finally: hub.disconnect(uid)

# app.websocket('/api/v1/ws')(websocket_endpoint)
# app.websocket('/ws')(websocket_endpoint)

# from __future__ import annotations
# import asyncio, os, logging
# from contextlib import asynccontextmanager
# from datetime import datetime, timezone
# from uuid import UUID
# import jwt
# from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.exceptions import HTTPException, RequestValidationError
# from fastapi.responses import JSONResponse
# from .core import settings, api_error_handler, engine, SessionLocal
# from .models import Base, User
# from sqlalchemy.exc import IntegrityError
# # Imports avant create_all: ils enregistrent tous les modèles dans Base.metadata.
# from .store import hub
# from .auth import router as auth_router
# from .api import router as api_router, auto_close_attendance_tick
# from .equipment_api import router as equipment_router
# from .chat_api import router as chat_router
# from .leaves_api import router as leaves_router
# from .sites_api import router as sites_router
# from .business_api import router as business_router
# from .presence_api import router as presence_router
# from .presence_api import presence_tick
# from .admin_api import router as admin_router

# event_queue: list[tuple[dict, set[str] | None]] = []
# logger=logging.getLogger("pointage")

# async def _dispatch_events():
#     while True:
#         pending = event_queue[:]
#         del event_queue[:]
#         for event, recipients in pending:
#             await hub.publish(event, recipients)
#         await asyncio.sleep(0.05)

# def _presence_tick_once():
#     db=SessionLocal()
#     try:
#         changed = presence_tick(db)
#         changed = auto_close_attendance_tick(db) or changed
#         if changed: db.commit()
#         else: db.rollback()
#     except Exception:
#         logger.exception("Échec d'une itération de contrôle de présence")
#         db.rollback()
#     finally: db.close()

# async def _presence_loop():
#     while True:
#         await asyncio.to_thread(_presence_tick_once)
#         await asyncio.sleep(10)

# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     if os.getenv("AUTO_CREATE_SCHEMA", "false").lower() == "true":
#         Base.metadata.create_all(engine)
#     if settings.environment.lower() == "production":
#         if len(settings.jwt_access_secret.encode()) < 32 or settings.jwt_access_secret == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET" or settings.jwt_access_secret.lower().startswith(("replace-with", "change_me", "changeme")):
#             raise RuntimeError("JWT_ACCESS_SECRET doit contenir au moins 32 octets aléatoires en production")
#         if len(settings.invite_code) < 16 or settings.invite_code == "CHANGE_ME" or settings.invite_code.lower().startswith(("replace-with", "change_me", "changeme")):
#             raise RuntimeError("INVITE_CODE doit contenir au moins 16 caractères en production")
#         if "change_me" in settings.database_url.lower() or "localhost" in settings.database_url.lower() or "127.0.0.1" in settings.database_url:
#             raise RuntimeError("DATABASE_URL doit désigner un serveur de base distant, non une valeur locale/exemple, en production")
#         if not settings.cookie_secure:
#             raise RuntimeError("COOKIE_SECURE=true est obligatoire en production")
#         if settings.cookie_samesite.lower() not in {"lax","strict","none"}:
#             raise RuntimeError("COOKIE_SAMESITE doit être Lax, Strict ou None")
#         if settings.cookie_samesite.lower()=="none" and not settings.cookie_secure:
#             raise RuntimeError("SameSite=None exige Secure")
#         if not origins or "*" in origins:
#             raise RuntimeError("FRONTEND_ORIGINS doit contenir une ou plusieurs origines explicites en production")
#         if any(origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1") for origin in origins):
#             raise RuntimeError("FRONTEND_ORIGINS contient encore une origine de développement local")
#         if os.getenv("AUTO_CREATE_SCHEMA","false").lower()=="true":
#             raise RuntimeError("AUTO_CREATE_SCHEMA doit rester désactivé en production; utiliser alembic upgrade head")
#     task=asyncio.create_task(_dispatch_events())
#     presence_task=asyncio.create_task(_presence_loop())
#     try:
#         yield
#     finally:
#         task.cancel()
#         presence_task.cancel()
#         try: await task
#         except asyncio.CancelledError: pass
#         try: await presence_task
#         except asyncio.CancelledError: pass

# app=FastAPI(title="Pointage Fortico API",version="2.0.0",description="API complète du frontend Pointage Fortico — FastAPI, PostgreSQL et WebSocket",lifespan=lifespan)
# origins=[x.strip() for x in settings.frontend_origins.split(',') if x.strip()]
# app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=True,allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],allow_headers=["Authorization","Content-Type","Accept"])
# app.add_exception_handler(HTTPException,api_error_handler)
# @app.exception_handler(IntegrityError)
# async def integrity_error(request:Request,exc:IntegrityError):
#     logger.info("Conflit de contrainte de données sur %s",request.url.path)
#     return JSONResponse(status_code=409,content={"error":{"code":"CONFLICT","message":"Cette modification entre en conflit avec des données déjà enregistrées.","details":None}})

# @app.exception_handler(Exception)
# async def unexpected_error(request:Request,exc:Exception):
#     logger.exception("Erreur interne sur %s",request.url.path)
#     return JSONResponse(status_code=500,content={"error":{"code":"INTERNAL_ERROR","message":"Une erreur interne est survenue.","details":None}})

# @app.exception_handler(RequestValidationError)
# async def validation_error(request:Request,exc:RequestValidationError):
#     fields=[]
#     for item in exc.errors():
#         fields.append({"field":".".join(str(x) for x in item.get("loc",())[1:]),"message":item.get("msg","Donnée invalide")})
#     return JSONResponse(status_code=400,content={"error":{"code":"VALIDATION_ERROR","message":"Les données envoyées sont invalides.","details":{"fields":fields}}})

# for route in (auth_router,api_router,equipment_router,chat_router,leaves_router,sites_router,business_router,presence_router,admin_router):
#     app.include_router(route,prefix="/api/v1")

# @app.get('/health')
# def health(): return {"status":"ok"}

# @app.get('/ready')
# def ready():
#     from sqlalchemy import text
#     with engine.connect() as connection: connection.execute(text("SELECT 1"))
#     return {"status":"ready","database":"ok"}

# async def websocket_endpoint(ws: WebSocket):
#     token=ws.query_params.get("token","")
#     try:
#         claims=jwt.decode(token,settings.jwt_access_secret,algorithms=["HS256"])
#         uid=UUID(claims["sub"])
#         db=SessionLocal()
#         user=db.get(User,uid)
#         if not user or not user.active or claims.get("ver")!=user.token_version:
#             db.close(); await ws.close(code=4401); return
#         if user.role.value=="employee":
#             from .models import Employee
#             employee=db.get(Employee,user.employee_code)
#             if not employee or not employee.active:
#                 db.close(); await ws.close(code=4401); return
#         expires=float(claims["exp"])
#         await hub.connect(ws,str(user.id),user.role.value,user.employee_code)
#         db.close()
#     except Exception:
#         try: await ws.close(code=4401)
#         except Exception: pass
#         return
#     uid=str(uid)
#     try:
#         while True:
#             remaining=expires-datetime.now(timezone.utc).timestamp()
#             if remaining<=0:
#                 await ws.close(code=4401); break
#             try: await asyncio.wait_for(ws.receive_text(),timeout=min(30,remaining))
#             except asyncio.TimeoutError: continue
#     except WebSocketDisconnect:
#         pass
#     except Exception:
#         pass
#     finally: hub.disconnect(uid)

# app.websocket('/api/v1/ws')(websocket_endpoint)
# app.websocket('/ws')(websocket_endpoint)


# from __future__ import annotations
# import asyncio, os, logging
# from contextlib import asynccontextmanager
# from datetime import datetime, timezone
# from uuid import UUID
# import jwt
# from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.exceptions import HTTPException, RequestValidationError
# from fastapi.responses import JSONResponse
# from .core import settings, api_error_handler, engine, SessionLocal
# from .models import Base, User
# from sqlalchemy.exc import IntegrityError
# # Imports avant create_all: ils enregistrent tous les modèles dans Base.metadata.
# from .store import hub
# from .auth import router as auth_router
# from .api import router as api_router
# from .equipment_api import router as equipment_router
# from .chat_api import router as chat_router
# from .leaves_api import router as leaves_router
# from .sites_api import router as sites_router
# from .business_api import router as business_router
# from .presence_api import router as presence_router
# from .presence_api import presence_tick
# from .admin_api import router as admin_router

# event_queue: list[tuple[dict, set[str] | None]] = []
# logger=logging.getLogger("pointage")

# async def _dispatch_events():
#     while True:
#         pending = event_queue[:]
#         del event_queue[:]
#         for event, recipients in pending:
#             await hub.publish(event, recipients)
#         await asyncio.sleep(0.05)

# def _presence_tick_once():
#     db=SessionLocal()
#     try:
#         if presence_tick(db): db.commit()
#         else: db.rollback()
#     except Exception:
#         logger.exception("Échec d'une itération de contrôle de présence")
#         db.rollback()
#     finally: db.close()

# async def _presence_loop():
#     while True:
#         await asyncio.to_thread(_presence_tick_once)
#         await asyncio.sleep(10)

# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     if os.getenv("AUTO_CREATE_SCHEMA", "false").lower() == "true":
#         Base.metadata.create_all(engine)
#     if settings.environment.lower() == "production":
#         if len(settings.jwt_access_secret.encode()) < 32 or settings.jwt_access_secret == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET" or settings.jwt_access_secret.lower().startswith(("replace-with", "change_me", "changeme")):
#             raise RuntimeError("JWT_ACCESS_SECRET doit contenir au moins 32 octets aléatoires en production")
#         if len(settings.invite_code) < 16 or settings.invite_code == "CHANGE_ME" or settings.invite_code.lower().startswith(("replace-with", "change_me", "changeme")):
#             raise RuntimeError("INVITE_CODE doit contenir au moins 16 caractères en production")
#         if "change_me" in settings.database_url.lower() or "localhost" in settings.database_url.lower() or "127.0.0.1" in settings.database_url:
#             raise RuntimeError("DATABASE_URL doit désigner un serveur de base distant, non une valeur locale/exemple, en production")
#         if not settings.cookie_secure:
#             raise RuntimeError("COOKIE_SECURE=true est obligatoire en production")
#         if settings.cookie_samesite.lower() not in {"lax","strict","none"}:
#             raise RuntimeError("COOKIE_SAMESITE doit être Lax, Strict ou None")
#         if settings.cookie_samesite.lower()=="none" and not settings.cookie_secure:
#             raise RuntimeError("SameSite=None exige Secure")
#         if not origins or "*" in origins:
#             raise RuntimeError("FRONTEND_ORIGINS doit contenir une ou plusieurs origines explicites en production")
#         if any(origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1") for origin in origins):
#             raise RuntimeError("FRONTEND_ORIGINS contient encore une origine de développement local")
#         if os.getenv("AUTO_CREATE_SCHEMA","false").lower()=="true":
#             raise RuntimeError("AUTO_CREATE_SCHEMA doit rester désactivé en production; utiliser alembic upgrade head")
#     task=asyncio.create_task(_dispatch_events())
#     presence_task=asyncio.create_task(_presence_loop())
#     try:
#         yield
#     finally:
#         task.cancel()
#         presence_task.cancel()
#         try: await task
#         except asyncio.CancelledError: pass
#         try: await presence_task
#         except asyncio.CancelledError: pass

# app=FastAPI(title="Pointage Fortico API",version="2.0.0",description="API complète du frontend Pointage Fortico — FastAPI, PostgreSQL et WebSocket",lifespan=lifespan)
# origins=[x.strip() for x in settings.frontend_origins.split(',') if x.strip()]
# app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=True,allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],allow_headers=["Authorization","Content-Type","Accept"])
# app.add_exception_handler(HTTPException,api_error_handler)
# @app.exception_handler(IntegrityError)
# async def integrity_error(request:Request,exc:IntegrityError):
#     logger.info("Conflit de contrainte de données sur %s",request.url.path)
#     return JSONResponse(status_code=409,content={"error":{"code":"CONFLICT","message":"Cette modification entre en conflit avec des données déjà enregistrées.","details":None}})

# @app.exception_handler(Exception)
# async def unexpected_error(request:Request,exc:Exception):
#     logger.exception("Erreur interne sur %s",request.url.path)
#     return JSONResponse(status_code=500,content={"error":{"code":"INTERNAL_ERROR","message":"Une erreur interne est survenue.","details":None}})

# @app.exception_handler(RequestValidationError)
# async def validation_error(request:Request,exc:RequestValidationError):
#     fields=[]
#     for item in exc.errors():
#         fields.append({"field":".".join(str(x) for x in item.get("loc",())[1:]),"message":item.get("msg","Donnée invalide")})
#     return JSONResponse(status_code=400,content={"error":{"code":"VALIDATION_ERROR","message":"Les données envoyées sont invalides.","details":{"fields":fields}}})

# for route in (auth_router,api_router,equipment_router,chat_router,leaves_router,sites_router,business_router,presence_router,admin_router):
#     app.include_router(route,prefix="/api/v1")

# @app.get('/health')
# def health(): return {"status":"ok"}

# @app.get('/ready')
# def ready():
#     from sqlalchemy import text
#     with engine.connect() as connection: connection.execute(text("SELECT 1"))
#     return {"status":"ready","database":"ok"}

# async def websocket_endpoint(ws: WebSocket):
#     token=ws.query_params.get("token","")
#     try:
#         claims=jwt.decode(token,settings.jwt_access_secret,algorithms=["HS256"])
#         uid=UUID(claims["sub"])
#         db=SessionLocal()
#         user=db.get(User,uid)
#         if not user or not user.active or claims.get("ver")!=user.token_version:
#             db.close(); await ws.close(code=4401); return
#         if user.role.value=="employee":
#             from .models import Employee
#             employee=db.get(Employee,user.employee_code)
#             if not employee or not employee.active:
#                 db.close(); await ws.close(code=4401); return
#         expires=float(claims["exp"])
#         await hub.connect(ws,str(user.id),user.role.value,user.employee_code)
#         db.close()
#     except Exception:
#         try: await ws.close(code=4401)
#         except Exception: pass
#         return
#     uid=str(uid)
#     try:
#         while True:
#             remaining=expires-datetime.now(timezone.utc).timestamp()
#             if remaining<=0:
#                 await ws.close(code=4401); break
#             try: await asyncio.wait_for(ws.receive_text(),timeout=min(30,remaining))
#             except asyncio.TimeoutError: continue
#     except WebSocketDisconnect:
#         pass
#     except Exception:
#         pass
#     finally: hub.disconnect(uid)

# app.websocket('/api/v1/ws')(websocket_endpoint)
# app.websocket('/ws')(websocket_endpoint)
