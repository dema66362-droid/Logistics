"""Логистик: минимальный серверный пилот (не юридическая система ЭПД)."""
from __future__ import annotations
import csv
import asyncio
import json
import hashlib
import hmac
import io
import math
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import (func, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer,
                        Numeric, String, Text, UniqueConstraint, create_engine, delete, event,
                        inspect, select, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.exc import SQLAlchemyError

BASE = Path(__file__).resolve().parent
DB_URL = os.environ.get('LOGISTIK_DATABASE_URL', f'sqlite:///{BASE / "data" / "logistik.sqlite3"}')
if DB_URL.startswith('postgres://'):
    DB_URL = 'postgresql://' + DB_URL[len('postgres://'):]
# SQLAlchemy 2.1 defaults to psycopg 3; this release installs psycopg2.
if DB_URL.startswith('postgresql://'):
    DB_URL = 'postgresql+psycopg2://' + DB_URL[len('postgresql://'):]
VERSION = '2.8.0'
BUSINESS_TZ = ZoneInfo(os.environ.get('LOGISTIK_TIMEZONE', 'Europe/Moscow'))

def business_today():
    return datetime.now(BUSINESS_TZ).date()
DEMO = os.environ.get('LOGISTIK_DEMO', '0') == '1'
IS_SQLITE = DB_URL.startswith('sqlite:')
DEPLOYMENT = os.environ.get('LOGISTIK_DEPLOYMENT', 'pilot').lower()
# This edition has NOT passed a public-production audit. Refuse that mode before touching the DB.
if DEPLOYMENT == 'production':
    raise RuntimeError('Unsafe production config: Логистик 2.3 is a private pilot only; '
                       'production deployment needs external security, migration, '
                       'PostgreSQL concurrency and operations verification')
if IS_SQLITE:
    (BASE / 'data').mkdir(exist_ok=True)
engine = create_engine(DB_URL, connect_args={'check_same_thread': False, 'timeout': 30} if IS_SQLITE else {}, pool_pre_ping=True)
if IS_SQLITE:
    @event.listens_for(engine, 'connect')
    def sqlite_options(dbapi_connection, _):
        dbapi_connection.execute('PRAGMA foreign_keys=ON')
        dbapi_connection.execute('PRAGMA busy_timeout=30000')
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
WRITE_LOCK = threading.RLock()
# One transaction-level lock for this small pilot, shared by ALL PostgreSQL workers.
# It also covers different orders competing for the same vehicle or driver.
PILOT_WRITE_LOCK = 741_203_627

class Base(DeclarativeBase): pass

class SchemaMigration(Base):
    __tablename__ = 'schema_migrations'
    version: Mapped[str] = mapped_column(String(20), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Organization(Base):
    __tablename__ = 'organizations'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20))

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    email: Mapped[str] = mapped_column(String(180), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

class ParticipantInvitation(Base):
    __tablename__ = 'participant_invitations'
    id: Mapped[int] = mapped_column(primary_key=True)
    sender_user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    sender_org_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    kind: Mapped[str] = mapped_column(String(20))
    target_email: Mapped[str] = mapped_column(String(180))
    target_role: Mapped[str] = mapped_column(String(20))
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class WebSession(Base):
    __tablename__ = 'web_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    csrf: Mapped[str] = mapped_column(String(80))
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class LoginThrottle(Base):
    __tablename__ = 'login_throttles'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

class ImportBatch(Base):
    __tablename__ = 'import_batches'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    order_refs: Mapped[str] = mapped_column(Text)

class Approval(Base):
    __tablename__ = 'approvals'
    id: Mapped[int] = mapped_column(primary_key=True)
    shipper_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint('shipper_id', 'carrier_id'),)

class Driver(Base):
    __tablename__ = 'drivers'
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    name: Mapped[str] = mapped_column(String(100))

class Vehicle(Base):
    __tablename__ = 'vehicles'
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    label: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    capacity: Mapped[Decimal] = mapped_column(Numeric(7,2))

class Shipment(Base):
    __tablename__ = 'shipments'
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(30), unique=True)
    shipper_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    from_city: Mapped[str] = mapped_column(String(120))
    to_city: Mapped[str] = mapped_column(String(120))
    recipient: Mapped[str] = mapped_column(String(140))
    product: Mapped[str] = mapped_column(String(180))
    tons: Mapped[Decimal] = mapped_column(Numeric(7,2))
    pickup_date: Mapped[date] = mapped_column(Date)
    pickup_time: Mapped[str] = mapped_column(String(5))
    unload_date: Mapped[date] = mapped_column(Date)
    vehicle_kind: Mapped[str] = mapped_column(String(30))
    mode: Mapped[str] = mapped_column(String(30))
    budget: Mapped[int] = mapped_column(Integer, default=0)
    rate: Mapped[int] = mapped_column(Integer, default=0)
    manager: Mapped[str] = mapped_column(String(100), default='')
    epd: Mapped[str] = mapped_column(String(80), default='')
    etrn: Mapped[str] = mapped_column(String(80), default='')
    note: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(30), default='search')
    hazardous: Mapped[bool] = mapped_column(Boolean, default=False)
    selected_offer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pickup_address: Mapped[str] = mapped_column(String(300), default='')
    delivery_address: Mapped[str] = mapped_column(String(300), default='')
    recipient_contact: Mapped[str] = mapped_column(String(200), default='')
    terms: Mapped[str] = mapped_column(Text, default='')
    terms_version: Mapped[int] = mapped_column(Integer, default=1)
    public_external: Mapped[bool] = mapped_column(Boolean, default=False)
    search_launched: Mapped[bool] = mapped_column(Boolean, default=False)
    tariff_source: Mapped[str] = mapped_column(String(30), default='manual')
    __table_args__ = (CheckConstraint('tons > 0'),)

class Offer(Base):
    __tablename__ = 'offers'
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey('shipments.id'))
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    driver_id: Mapped[int] = mapped_column(ForeignKey('drivers.id'))
    vehicle_id: Mapped[int] = mapped_column(ForeignKey('vehicles.id'))
    price: Mapped[int] = mapped_column(Integer)
    pickup_date: Mapped[date] = mapped_column(Date)
    pickup_time: Mapped[str] = mapped_column(String(5))
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default='waiting_driver')
    terms_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted_terms_version: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint('price > 0'),)

class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[int] = mapped_column(Integer)
    shipment_id: Mapped[int] = mapped_column(ForeignKey('shipments.id'))
    action: Mapped[str] = mapped_column(String(60))
    details: Mapped[str] = mapped_column(Text, default='')
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Notification(Base):
    __tablename__='notifications'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey('shipments.id'), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(String(300))
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

def initialize_schema():
    """Idempotent additive pilot migration, serialized against other app workers."""
    with engine.begin() as conn:
        if IS_SQLITE:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        elif engine.dialect.name == 'postgresql':
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': PILOT_WRITE_LOCK})
        Base.metadata.create_all(conn)
        latest = conn.scalar(select(func.max(SchemaMigration.version)))
        if latest and latest > '0028':
            raise RuntimeError('Database schema is newer than this application release')
        for table, definitions in {
            'shipments': {'pickup_address':"VARCHAR(300) NOT NULL DEFAULT ''",'delivery_address':"VARCHAR(300) NOT NULL DEFAULT ''",'recipient_contact':"VARCHAR(200) NOT NULL DEFAULT ''",'terms':"TEXT NOT NULL DEFAULT ''",'terms_version':'INTEGER NOT NULL DEFAULT 1','public_external':'BOOLEAN NOT NULL DEFAULT 0','search_launched':'BOOLEAN NOT NULL DEFAULT 0','tariff_source':"VARCHAR(30) NOT NULL DEFAULT 'manual'"},
            'offers': {'terms_accepted':'BOOLEAN NOT NULL DEFAULT 0','accepted_terms_version':'INTEGER NOT NULL DEFAULT 0'}
        }.items():
            fields={column['name'] for column in inspect(conn).get_columns(table)}
            for col,defn in definitions.items():
                if col not in fields:
                    if not IS_SQLITE: defn = defn.replace('BOOLEAN NOT NULL DEFAULT 0', 'BOOLEAN NOT NULL DEFAULT FALSE')
                    conn.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {col} {defn}')
        if not conn.scalar(select(SchemaMigration.version).where(SchemaMigration.version == '0027')):
            conn.execute(SchemaMigration.__table__.insert().values(version='0027', applied_at=datetime.now(timezone.utc)))
        if 'is_admin' not in {c['name'] for c in inspect(conn).get_columns('users')}:
            default = '0' if IS_SQLITE else 'FALSE'
            conn.exec_driver_sql('ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT ' + default)
            conn.execute(text("UPDATE users SET is_admin = :yes WHERE role IN ('shipper','carrier')"), {'yes': True})
        if not conn.scalar(select(SchemaMigration.version).where(SchemaMigration.version == '0028')):
            conn.execute(SchemaMigration.__table__.insert().values(version='0028', applied_at=datetime.now(timezone.utc)))

initialize_schema()
app = FastAPI(title='Логистик MVP', docs_url=None, redoc_url=None, openapi_url=None)

@app.exception_handler(SQLAlchemyError)
async def database_unavailable(request: Request, exc: SQLAlchemyError):
    # Never return SQL, connection strings, parameters or stack traces to a visitor.
    return JSONResponse(status_code=503, content={'detail': 'Хранилище временно недоступно. Обновите данные и повторите действие.'})
trusted_hosts = [h.strip() for h in os.environ.get('LOGISTIK_ALLOWED_HOSTS','').split(',') if h.strip()]
if trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

@app.middleware('http')
async def privacy_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
    return response

@contextmanager
def db_write():
    with WRITE_LOCK:
        with SessionLocal() as db:
            if IS_SQLITE:
                db.execute(text('BEGIN IMMEDIATE'))
            elif engine.dialect.name == 'postgresql':
                db.execute(text("SET LOCAL lock_timeout = '8s'"))
                db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': PILOT_WRITE_LOCK})
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

def pass_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 350_000)
    return salt.hex() + ':' + key.hex()

def pass_ok(password: str, encoded: str) -> bool:
    try:
        salt_hex, value_hex = encoded.split(':', 1)
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt_hex), 350_000)
        return hmac.compare_digest(actual, bytes.fromhex(value_hex))
    except (ValueError, TypeError):
        return False

def sha_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def forbidden(msg='Недостаточно прав'):
    raise HTTPException(status_code=403, detail=msg)

def invalid(msg='Недопустимое действие', code=409):
    raise HTTPException(status_code=code, detail=msg)

def audit(db: Session, actor: User, shipment: Shipment, action: str, details: str = ''):
    db.add(Audit(org_id=actor.org_id, actor_id=actor.id, shipment_id=shipment.id, action=action, details=details))

def note_user(db:Session, user_id:int, shipment_id:int|None, kind:str, title:str, msg:str):
    db.add(Notification(user_id=user_id, shipment_id=shipment_id, kind=kind, title=title, message=msg))

def note_org(db:Session, org_id:int, role:str, shipment_id:int|None, kind:str, title:str, msg:str):
    for u in db.scalars(select(User).where(User.org_id==org_id,User.role==role,User.active.is_(True))):
        note_user(db,u.id,shipment_id,kind,title,msg)

def fleet_suitable(db:Session, shipment:Shipment, carrier_id:int)->bool:
    return db.scalar(select(Vehicle.id).where(Vehicle.org_id==carrier_id,Vehicle.kind==shipment.vehicle_kind,Vehicle.capacity>=shipment.tons).limit(1)) is not None

def public_visible(db:Session, shipment:Shipment, carrier_id:int)->bool:
    return (shipment.public_external and shipment.search_launched and not shipment.hazardous
            and shipment.status in ('search','choice') and fleet_suitable(db,shipment,carrier_id))

def allowed(db: Session, shipment: Shipment, carrier_id: int) -> bool:
    return db.scalar(select(Approval).where(Approval.shipper_id == shipment.shipper_id,
                                           Approval.carrier_id == carrier_id, Approval.active.is_(True))) is not None

def protect_csrf(request: Request, csrf: str):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        forbidden('Источник запроса не совпадает с адресом сайта')
    if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), csrf):
        forbidden('Сессия требует обновления (CSRF)')

def user_session(request: Request, logistik_session: str | None = Cookie(None)):
    if not logistik_session:
        raise HTTPException(401, detail='Необходим вход')
    with SessionLocal() as db:
        websession = db.get(WebSession, sha_token(logistik_session))
        if not websession or websession.expires.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise HTTPException(401, detail='Сессия истекла')
        user = db.get(User, websession.user_id)
        if not user or not user.active:
            raise HTTPException(401, detail='Доступ закрыт')
        return dict(id=user.id, org_id=user.org_id, role=user.role, name=user.name,
                    email=user.email, is_admin=user.is_admin, csrf=websession.csrf)

class CarrierSignup(BaseModel):
    organization: str = Field(min_length=3,max_length=160)
    name: str = Field(min_length=2,max_length=100)
    email: str = Field(min_length=6,max_length=180)
    password: str = Field(min_length=12,max_length=128)
    vehicle_kind: Literal['Автоцистерна','Фура']
    capacity_tons: Decimal = Field(gt=0,le=100,max_digits=7,decimal_places=2)
    vehicle_label: str = Field(min_length=3,max_length=100)

@app.post('/api/signup/carrier',status_code=201)
def register_carrier(payload:CarrierSignup, request:Request, response:Response):
    # Preserve clients of the previous carrier-only API.
    return register_account(Signup(role='carrier', **payload.model_dump()), request, response)

class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=180)
    password: str = Field(min_length=1, max_length=128)

# Equal work for an unknown email and an incorrect password.
UNKNOWN_PASSWORD_HASH = pass_hash(secrets.token_urlsafe(32))

@app.post('/api/login')
def login(payload: LoginIn, request: Request, response: Response):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        forbidden('Источник входа не совпадает с адресом сайта')
    email = payload.email.strip().lower().removeprefix('mailto:')
    host = request.client.host if request.client else 'unknown'
    keys = [(sha_token('ip:' + host), 40), (sha_token('account:' + email), 8)]
    now = datetime.now(timezone.utc)
    token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(28)
    denied = 0
    with db_write() as db:
        db.execute(delete(LoginThrottle).where(LoginThrottle.expires < now))
        buckets = [(db.get(LoginThrottle, key), key, limit) for key, limit in keys]
        if any(bucket and bucket.failures >= limit for bucket, _, limit in buckets):
            denied = 429
        else:
            user = db.scalar(select(User).where(User.email == email))
            password_matches = pass_ok(payload.password, user.password_hash if user else UNKNOWN_PASSWORD_HASH)
            if not user or not user.active or not password_matches:
                denied = 401
                for bucket, key, _ in buckets:
                    if bucket is None:
                        db.add(LoginThrottle(key=key, failures=1, expires=now+timedelta(minutes=5)))
                    else:
                        bucket.failures += 1
            else:
                db.execute(delete(WebSession).where(WebSession.expires < now))
                db.add(WebSession(token_hash=sha_token(token), user_id=user.id, csrf=csrf,
                                  expires=now+timedelta(days=7)))
    # Failed attempts must COMMIT before the error response is raised.
    if denied:
        raise HTTPException(denied, detail='Много попыток. Повторите через 5 минут' if denied == 429 else 'Неверные данные входа')
    response.set_cookie('logistik_session', token, httponly=True, samesite='strict',
                        secure=os.getenv('LOGISTIK_SECURE_COOKIE','0')=='1', max_age=7*86400, path='/')
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True}

@app.post('/api/logout')
def logout(request: Request, response: Response, user=Depends(user_session), logistik_session: str | None=Cookie(None)):
    protect_csrf(request, user['csrf'])
    with db_write() as db:
        ws = db.get(WebSession, sha_token(logistik_session or ''))
        if ws: db.delete(ws)
    response.delete_cookie('logistik_session', path='/')
    return {'ok': True}

class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=14, max_length=128)

@app.post('/api/account/password')
def change_own_password(payload: PasswordChangeIn, request: Request,
                        response: Response, user=Depends(user_session)):
    """Current password required; revoke every active session after the change."""
    protect_csrf(request, user['csrf'])
    if payload.current_password == payload.new_password:
        invalid('Новый пароль должен отличаться от старого', 422)
    with db_write() as db:
        account = db.get(User, user['id'])
        if not account or not account.active or not pass_ok(payload.current_password, account.password_hash):
            raise HTTPException(403, detail='Текущий пароль указан неверно')
        account.password_hash = pass_hash(payload.new_password)
        db.execute(text('DELETE FROM web_sessions WHERE user_id = :user_id'), {'user_id': account.id})
    response.delete_cookie('logistik_session', path='/', secure=os.getenv('LOGISTIK_SECURE_COOKIE','0')=='1', samesite='strict')
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True, 'message': 'Пароль изменён. Войдите заново.'}

@app.get('/health')
def health():
    # A real database probe, not just a response from the web worker.
    try:
        with engine.connect() as con:
            con.execute(text('SELECT 1'))
    except Exception:
        raise HTTPException(status_code=503, detail='Хранилище недоступно')
    return {'status': 'ok', 'service': 'logistik', 'version': VERSION}

@app.get('/')
def root():
    return FileResponse(BASE/'static'/'index.html', headers={'Cache-Control': 'no-store', 'X-Content-Type-Options':'nosniff', 'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'self'; object-src 'none'; frame-ancestors 'none'"})

def shipment_dict(s: Shipment, db: Session):
    chosen=db.get(Offer,s.selected_offer_id) if s.selected_offer_id else None
    carrier=db.get(Organization,chosen.carrier_id) if chosen else None
    driver=db.get(Driver,chosen.driver_id) if chosen else None
    vehicle=db.get(Vehicle,chosen.vehicle_id) if chosen else None
    return {'id':s.ref,'date':s.pickup_date.isoformat(),'time':s.pickup_time,'unload':s.unload_date.isoformat(),
            'product':s.product,'tons':float(s.tons),'from':s.from_city,'to':s.to_city,
            'pickupAddress':s.pickup_address,'deliveryAddress':s.delivery_address,
            'recipientContact':s.recipient_contact,'terms':s.terms,'termsVersion':s.terms_version,
            'publicExternal':s.public_external,'searchLaunched':s.search_launched,'tariffSource':s.tariff_source,
            'recipient':s.recipient,'manager':s.manager,'vehicleType':s.vehicle_kind,'mode':s.mode,
            'status':s.status,'budget':s.budget,
            'rate':int((Decimal(chosen.price)/s.tons).quantize(Decimal('1'),rounding=ROUND_HALF_UP)) if chosen else s.rate,
            'price':chosen.price if chosen else 0,
            'carrier':carrier.name if carrier else '', 'driver':driver.name if driver else '',
            'car':vehicle.label if vehicle else '', 'epd':s.epd,'etrn':s.etrn,'note':s.note,
            'selectedOffer':f'Q-{chosen.id}' if chosen else None,'hazardous':s.hazardous}

def sanitize_for_role(item:dict, role:str, *, external:bool=False, assigned:bool=False):
    if external:
        # Explicit allowlist: no exact address, recipient, contact, manager, budget, shipper identity, or offers.
        keys=('id','date','time','unload','product','tons','from','to','vehicleType','mode','status','terms','termsVersion','hazardous')
        return {k:item[k] for k in keys}
    if role=='driver':
        item.update(budget=0,rate=0,price=0,manager='',epd='',etrn='',note='',terms='',recipientContact=item['recipientContact'] if assigned else '',
                    pickupAddress=item['pickupAddress'] if assigned else '',deliveryAddress=item['deliveryAddress'] if assigned else '',
                    recipient=item['recipient'] if assigned else '',tariffSource='',publicExternal=False)
    return item

def offer_dict(o: Offer, db: Session):
    return {'id':f'Q-{o.id}','orderId':db.get(Shipment,o.shipment_id).ref,
            'carrier':db.get(Organization,o.carrier_id).name,'carrierId':o.carrier_id,
            'driver':db.get(Driver,o.driver_id).name,'car':db.get(Vehicle,o.vehicle_id).label,
            'driverId':o.driver_id,'vehicleId':o.vehicle_id,'price':o.price,'pickup':o.pickup_time,
            'day':o.pickup_date.isoformat(),'endDate':o.end_date.isoformat(),
            'status':o.status,'termsAccepted':o.terms_accepted,'approved':allowed(db,db.get(Shipment,o.shipment_id),o.carrier_id),
            'duration':'время — по графику'}

@app.get('/api/state')
def get_state(user=Depends(user_session)):
    with SessionLocal() as db:
        if user['role']=='shipper':
            shipments=db.scalars(select(Shipment).where(Shipment.shipper_id==user['org_id']).order_by(Shipment.pickup_date)).all()
            visible_ids=[s.id for s in shipments]
            offers=db.scalars(select(Offer).where(Offer.shipment_id.in_(visible_ids))).all() if visible_ids else []
            drivers=[]; vehicles=[]
        elif user['role']=='carrier':
            approved=db.scalars(select(Approval).where(Approval.carrier_id==user['org_id'],Approval.active.is_(True))).all()
            ships={x.shipper_id for x in approved}
            shipments=db.scalars(select(Shipment).where(Shipment.shipper_id.in_(ships),Shipment.search_launched.is_(True)).order_by(Shipment.pickup_date)).all() if ships else []
            public=db.scalars(select(Shipment).where(Shipment.public_external.is_(True),Shipment.search_launched.is_(True),Shipment.hazardous.is_(False),Shipment.status.in_(['search','choice']))).all()
            # Own assigned work remains accessible even if a shipper later revokes pool access.
            own_assigned=db.scalars(select(Shipment).join(Offer,Offer.id==Shipment.selected_offer_id)
                .where(Offer.carrier_id==user['org_id'])).all()
            shipments.extend(own_assigned)
            keys={s.id for s in shipments}
            # Hide accepted assignments of OTHER carriers, including their names and drivers.
            shipments=[s for s in shipments if
                ((s.status in ('search','choice') and allowed(db,s,user['org_id'])
                  and fleet_suitable(db,s,user['org_id'])) or
                 (s.selected_offer_id and db.get(Offer,s.selected_offer_id).carrier_id==user['org_id']))]
            shipments += [s for s in public if s.id not in keys and public_visible(db,s,user['org_id'])]
            shipments=list({s.id:s for s in shipments}.values())
            offers=db.scalars(select(Offer).where(Offer.carrier_id==user['org_id'])).all()
            drivers=db.scalars(select(Driver).join(User,User.id==Driver.user_id).where(Driver.org_id==user['org_id'],User.active.is_(True))).all()
            vehicles=db.scalars(select(Vehicle).where(Vehicle.org_id==user['org_id'])).all()
        elif user['role']=='driver':
            driver=db.scalar(select(Driver).where(Driver.user_id==user['id']))
            offers=db.scalars(select(Offer).where(Offer.driver_id==driver.id)).all() if driver else []
            ids=[o.shipment_id for o in offers if o.status in ('waiting_driver','driver_ok','selected')]
            shipments=db.scalars(select(Shipment).where(Shipment.id.in_(ids))).all() if ids else []
            drivers=[driver] if driver else []
            vehicles=db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in offers]))).all() if offers else []
        else: forbidden()
        result=[]
        for s in shipments:
            item=shipment_dict(s,db)
            carrier_assigned=(s.selected_offer_id and db.get(Offer,s.selected_offer_id).carrier_id==user['org_id']) if user['role']=='carrier' else False
            if user['role']=='carrier' and not allowed(db,s,user['org_id']) and not carrier_assigned:
                item=sanitize_for_role(item,'carrier',external=True)
            elif user['role']=='driver':
                assigned=any(o.shipment_id==s.id and o.status=='selected' for o in offers)
                item=sanitize_for_role(item,'driver',assigned=assigned)
            result.append(item)
        notes=db.scalars(select(Notification).where(Notification.user_id==user['id']).order_by(Notification.id.desc()).limit(50)).all()
        current_org=db.get(Organization,user['org_id'])
        return {'user':{**{k:v for k,v in user.items() if k!='csrf'},
                        'organizationName':current_org.name if current_org else ''},'csrf':user['csrf'],
                'orders':result, 'offers':[({**offer_dict(o,db), 'price':0} if user['role']=='driver' else offer_dict(o,db)) for o in offers],
                'drivers':[{'id':d.id,'name':d.name,'userId':d.user_id} for d in drivers],
                'vehicles':[{'id':v.id,'label':v.label,'kind':v.kind,'capacity':float(v.capacity)} for v in vehicles],
                'notifications':[{'id':n.id,'kind':n.kind,'title':n.title,'message':n.message,'read':n.read,'orderId':db.get(Shipment,n.shipment_id).ref if n.shipment_id else None} for n in notes],
                'day':business_today().isoformat()}

@app.get('/api/events')
async def events(request:Request,user=Depends(user_session)):
    # Foreground change feed, not background push. No personal details are in the event payload.
    try: after=max(0,int(request.query_params.get('after','0')))
    except ValueError:after=0
    async def stream():
        last=after
        token_hash = sha_token(request.cookies.get('logistik_session', ''))
        for i in range(29):
            if await request.is_disconnected():break
            with SessionLocal() as db:
                session = db.get(WebSession, token_hash)
                account = db.get(User, user['id'])
                if (not session or session.expires.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)
                        or not account or not account.active):
                    yield 'event: signed_out\ndata: {}\n\n'
                    return
                top=db.scalar(select(func.max(Notification.id)).where(Notification.user_id==user['id'])) or 0
            if top>last:
                last=top
                yield f'event: changes\ndata: {last}\n\n'
            elif i%10==0:
                yield ': keepalive\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no','X-Content-Type-Options':'nosniff'})

@app.post('/api/notifications/{note_id}/read')
def read_note(note_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        note=db.get(Notification,note_id)
        if not note or note.user_id!=user['id']:raise HTTPException(404,detail='Уведомление не найдено')
        note.read=True
    return {'ok':True}

class CreateShipment(BaseModel):
    from_city: str = Field(min_length=2,max_length=120)
    to_city: str = Field(min_length=2,max_length=120)
    recipient: str = Field(min_length=2,max_length=140)
    product: str = Field(min_length=2,max_length=180)
    tons: Decimal = Field(gt=0,le=100,max_digits=7,decimal_places=2)
    date: date
    time: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    unload: date | None = None
    vehicle_type: Literal['Автоцистерна','Фура']
    mode: Literal['Доставка','Самовывоз']
    budget: int = Field(ge=0,le=50_000_000,default=0)
    manager: str = Field(default='',max_length=100)
    note: str = Field(default='',max_length=2000)
    hazardous: bool = False
    pickup_address: str = Field(default='', max_length=300)
    delivery_address: str = Field(default='', max_length=300)
    recipient_contact: str = Field(default='', max_length=200)
    terms: str = Field(default='', max_length=2000)
    public_external: bool = False
    tariff_per_ton: int = Field(ge=0,le=2_000_000,default=0)
    price_basis: Literal['per_ton','per_trip']='per_trip'
    @field_validator('from_city','to_city','recipient','product', mode='before')
    @classmethod
    def trim(cls,v): return v.strip() if isinstance(v, str) else v

@app.post('/api/orders',status_code=201)
def create_shipment(payload: CreateShipment, request: Request, user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper': forbidden()
    s = build_shipment(payload, user)
    with db_write() as db:
        db.add(s);db.flush();audit(db,db.get(User,user['id']),s,'created',f'hazardous={s.hazardous}')
        result=shipment_dict(s,db)
    return result


def build_shipment(payload: CreateShipment, user: dict) -> Shipment:
    """The same validation and money rules apply to the form and CSV import."""
    if payload.date < business_today(): invalid('Дата погрузки не может быть в прошлом', 422)
    if payload.unload and payload.unload < payload.date: invalid('Выгрузка раньше погрузки',422)
    if payload.price_basis=='per_ton' and payload.tariff_per_ton:
        payload.budget=int((payload.tons*Decimal(payload.tariff_per_ton)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    elif payload.budget:
        payload.tariff_per_ton=int((Decimal(payload.budget)/payload.tons).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    elif payload.tariff_per_ton:
        payload.budget=int((payload.tons*Decimal(payload.tariff_per_ton)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    if payload.budget > 50_000_000: invalid('Стоимость рейса превышает допустимый предел', 422)
    if not payload.terms.strip():invalid('Необходимо указать требования к перевозке',422)
    # The pilot cannot establish dangerous-goods classification from free text.
    # Only explicit inert demo categories can go through the trial assignment workflow.
    safe_demo_cargo={'вода техническая', 'вода питьевая', 'пустая тара'}
    hazardous=payload.hazardous or payload.product.strip().lower() not in safe_demo_cargo
    # A dispatchable test shipment must carry enough information for a driver to reach both places.
    # Unknown/hazardous goods remain blocked drafts in this pilot.
    if not hazardous:
        missing = [field for field, value in (
            ('точный адрес погрузки', payload.pickup_address),
            ('точный адрес доставки', payload.delivery_address if payload.mode == 'Доставка' else 'Не требуется при самовывозе'),
            ('контакт грузополучателя', payload.recipient_contact if payload.mode == 'Доставка' else 'Не требуется при самовывозе'),
        ) if len(value.strip()) < 3]
        if missing: invalid('Для оформления заявки заполните: ' + ', '.join(missing), 422)
    return Shipment(ref=f'NEW-{secrets.token_hex(6).upper()}',shipper_id=user['org_id'],
            from_city=payload.from_city,to_city=payload.to_city,recipient=payload.recipient,
            product=payload.product,tons=payload.tons,pickup_date=payload.date,pickup_time=payload.time,
            unload_date=payload.unload or payload.date,vehicle_kind=payload.vehicle_type,mode=payload.mode,
            budget=payload.budget,rate=payload.tariff_per_ton,manager=payload.manager or user['name'],note=payload.note,
            hazardous=hazardous,status='search',epd='',etrn='',pickup_address=payload.pickup_address.strip(),
            delivery_address=payload.delivery_address.strip(),recipient_contact=payload.recipient_contact.strip(),
            terms=payload.terms.strip(),public_external=payload.public_external and not hazardous,search_launched=False,
            tariff_source='manual')


class CsvImportIn(BaseModel):
    csv: str = Field(min_length=1, max_length=512_000)


@app.post('/api/import.csv', status_code=201)
def import_orders(payload: CsvImportIn, request: Request, response: Response, user=Depends(user_session)):
    protect_csrf(request, user['csrf'])
    if user['role'] != 'shipper': forbidden()
    reader = csv.DictReader(io.StringIO(payload.csv.lstrip('\ufeff')), delimiter=';', strict=True)
    try:
        fields = reader.fieldnames or []
    except csv.Error:
        invalid('Неверный формат заголовка CSV', 422)
    if not fields or len(fields) != len(set(fields)) or not set(fields) <= set(CreateShipment.model_fields):
        invalid('Заголовки CSV не соответствуют шаблону импорта', 422)
    prepared = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if len(prepared) >= 300: invalid('За один импорт допускается не более 300 заявок', 422)
            if None in row or None in row.values(): invalid(f'Строка {row_number}: неверное число колонок', 422)
            values = {key: value.strip() for key, value in row.items() if value.strip()}
            try:
                model = CreateShipment.model_validate(values)
            except ValidationError as exc:
                names = ', '.join(str(error['loc'][0]) for error in exc.errors())
                invalid(f'Строка {row_number}: проверьте поля {names}', 422)
            try:
                prepared.append(build_shipment(model, user))
            except HTTPException as exc:
                invalid(f'Строка {row_number}: {exc.detail}', 422)
    except csv.Error:
        invalid('Неверный формат CSV', 422)
    if not prepared: invalid('В файле нет заявок', 422)
    key = sha_token(str(user['org_id']) + ':' + payload.csv)
    with db_write() as db:
        previous = db.get(ImportBatch, key)
        if previous:
            response.status_code = 200
            return {'created': 0, 'replayed': True, 'orderIds': json.loads(previous.order_refs)}
        refs = []
        for shipment in prepared:
            db.add(shipment);db.flush()
            audit(db, db.get(User, user['id']), shipment, 'imported', 'CSV: непубликуемый черновик')
            refs.append(shipment.ref)
        db.add(ImportBatch(key=key, org_id=user['org_id'], order_refs=json.dumps(refs)))
    return {'created': len(refs), 'replayed': False, 'orderIds': refs}

def get_visible_shipment(db:Session, ref:str, user:dict):
    s=db.scalar(select(Shipment).where(Shipment.ref==ref))
    if not s: raise HTTPException(404,detail='Заявка не найдена')
    if user['role']=='shipper' and s.shipper_id==user['org_id']: return s
    if user['role']=='carrier' and (allowed(db,s,user['org_id']) or public_visible(db,s,user['org_id'])): return s
    forbidden()

class CreateOffer(BaseModel):
    order_ref:str
    driver_id:int
    vehicle_id:int
    day:date
    pickup:str=Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    end_date:date
    price:int=Field(ge=1000,le=50_000_000)
    accept_terms: bool = False


def intersects(a1:date,a2:date,b1:date,b2:date):
    return a1<=b2 and b1<=a2

def conflicts(db:Session, driver_id:int, vehicle_id:int, start:date, end:date,
              exclude_shipment:int|None=None, *, include_driver_accepted:bool=False):
    # Confirmed journeys reserve resources; optionally reserve a driver's accepted pending bids.
    confirmed=db.scalars(select(Offer).join(Shipment,Shipment.selected_offer_id==Offer.id)
            .where(Shipment.status.in_(['confirmed','enroute']))).all()
    for q in confirmed:
        if exclude_shipment and q.shipment_id==exclude_shipment:continue
        if (q.driver_id==driver_id or q.vehicle_id==vehicle_id) and intersects(start,end,q.pickup_date,q.end_date):return True
    if include_driver_accepted:
        held=db.scalars(select(Offer).join(Shipment,Shipment.id==Offer.shipment_id)
            .where(Offer.status=='driver_ok',Shipment.status.in_(['search','choice']))).all()
        for q in held:
            if exclude_shipment and q.shipment_id==exclude_shipment:continue
            if (q.driver_id==driver_id or q.vehicle_id==vehicle_id) and intersects(start,end,q.pickup_date,q.end_date):return True
    return False

@app.post('/api/offers',status_code=201)
def create_offer(payload:CreateOffer,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier':forbidden()
    with db_write() as db:
        s=get_visible_shipment(db,payload.order_ref,user)
        if s.status not in ('search','choice'):invalid('Заявка закрыта для предложений')
        if s.hazardous:invalid('Для опасного груза назначение заблокировано до внедрения проверки допусков')
        if not s.search_launched:invalid('Заказчик ещё не запустил поиск машины')
        if not payload.accept_terms or not s.terms.strip(): invalid('Необходимо согласие с требованиями заказчика',422)
        existing=db.scalar(select(Offer).where(Offer.shipment_id==s.id,Offer.carrier_id==user['org_id'],Offer.status.in_(['waiting_driver','driver_ok','selected'])))
        if existing:invalid('Ваша организация уже подала предложение по заявке')
        d=db.get(Driver,payload.driver_id);v=db.get(Vehicle,payload.vehicle_id)
        if not d or not v or d.org_id!=user['org_id'] or v.org_id!=user['org_id']:forbidden('Чужой водитель или автомобиль')
        if v.kind!=s.vehicle_kind or v.capacity<s.tons:invalid('Тип или грузоподъёмность транспорта не соответствуют заявке')
        if payload.end_date<payload.day:invalid('Конец рейса раньше начала',422)
        if payload.day < business_today():invalid('Дата подачи не может быть в прошлом',422)
        if payload.end_date<s.unload_date:invalid('Окончание занятости не может быть раньше плановой выгрузки',422)
        if conflicts(db,d.id,v.id,payload.day,payload.end_date,include_driver_accepted=True):invalid('Автомобиль или водитель заняты либо зарезервированы в указанный период')
        q=Offer(shipment_id=s.id,carrier_id=user['org_id'],driver_id=d.id,vehicle_id=v.id,price=payload.price,
                pickup_date=payload.day,pickup_time=payload.pickup,end_date=payload.end_date,status='waiting_driver',
                terms_accepted=True,accepted_terms_version=s.terms_version)
        db.add(q);db.flush();audit(db,db.get(User,user['id']),s,'offer_created',f'offer={q.id}')
        note_user(db,db.get(Driver,d.id).user_id,s.id,'driver_request','Запрос водителю',f'Подтвердите доступность на {payload.day}') if d.user_id else None
        result=offer_dict(q,db)
    return result

def assign_offer(db:Session, q:Offer, s:Shipment, actor:User, action:str):
    if s.status not in ('search','choice') or s.selected_offer_id is not None: invalid('Заявка уже назначена')
    if q.status!='driver_ok':invalid('Нет согласия водителя')
    if s.hazardous:invalid('Для опасного груза требуется отдельная проверка допусков')
    if not allowed(db,s,q.carrier_id): invalid('Новый перевозчик ещё не принят в пул заказчика')
    if not q.terms_accepted or q.accepted_terms_version!=s.terms_version:invalid('Требования заказчика не приняты')
    v=db.get(Vehicle,q.vehicle_id)
    if not v or v.kind!=s.vehicle_kind or v.capacity<s.tons: invalid('Автомобиль не соответствует заявке')
    driver = db.get(Driver, q.driver_id)
    account = db.get(User, driver.user_id) if driver and driver.user_id else None
    if not driver or not account or not account.active:invalid('Учётная запись водителя недоступна')
    if q.pickup_date < business_today():invalid('Дата подачи уже прошла. Требуется новое предложение')
    if conflicts(db,q.driver_id,q.vehicle_id,q.pickup_date,q.end_date,exclude_shipment=s.id,include_driver_accepted=True):invalid('Машина или водитель уже заняты')
    s.selected_offer_id=q.id;s.status='confirmed';q.status='selected'
    for r in db.scalars(select(Offer).where(Offer.shipment_id==s.id,Offer.id!=q.id)):
        r.status='not_selected'
        note_org(db,r.carrier_id,'carrier',s.id,'not_selected','Заявка закрыта',f'Заявка {s.ref} назначена другому перевозчику')
        rejected_driver=db.get(Driver,r.driver_id)
        if rejected_driver.user_id:
            note_user(db,rejected_driver.user_id,s.id,'not_selected','Рейс не назначен',
                      f'{s.ref}: заказчик выбрал другое предложение')
    audit(db,actor,s,action,f'offer={q.id}')
    note_org(db,s.shipper_id,'shipper',s.id,'assigned','Машина назначена',f'{s.ref}: подтверждены перевозчик и водитель')
    note_org(db,q.carrier_id,'carrier',s.id,'assigned','Заказ назначен',f'Заявка {s.ref} включена в график')
    d=db.get(Driver,q.driver_id)
    if d.user_id:note_user(db,d.user_id,s.id,'assigned','Рейс назначен',f'{s.ref}: точный адрес и контакт появились в графике')
    return shipment_dict(s,db)

@app.post('/api/offers/{offer_id}/driver-response')
def driver_response(offer_id:int,accepted:bool,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='driver':forbidden()
    with db_write() as db:
        q=db.get(Offer,offer_id)
        driver=db.scalar(select(Driver).where(Driver.user_id==user['id']))
        if not q or not driver or driver.id!=q.driver_id:forbidden('Это не ваше предложение')
        s=db.get(Shipment,q.shipment_id)
        if q.status!='waiting_driver':invalid('Предложение уже обработано')
        if s.status not in ('search','choice'):invalid('Заявка закрыта')
        if s.hazardous:invalid('Для опасного груза назначение заблокировано')
        if accepted and q.pickup_date < business_today():invalid('Дата подачи уже прошла')
        if accepted and conflicts(db,q.driver_id,q.vehicle_id,q.pickup_date,q.end_date,
                                  exclude_shipment=s.id,include_driver_accepted=True):
            invalid('Водитель или автомобиль уже заняты либо подтвердили пересекающееся предложение')
        q.status='driver_ok' if accepted else 'declined'
        if accepted:s.status='choice'
        audit(db,db.get(User,user['id']),s,'driver_ok' if accepted else 'driver_declined',f'offer={q.id}')
        note_org(db,q.carrier_id,'carrier',s.id,'driver_ok' if accepted else 'driver_declined',
                 'Ответ водителя',f'{s.ref}: водитель {"подтвердил" if accepted else "отклонил"} рейс')
        if (accepted and allowed(db,s,q.carrier_id) and q.price==s.budget and s.budget>0
                and (q.pickup_date,q.pickup_time)<=(s.pickup_date,s.pickup_time)):
            # First confirmed driver wins at the published price, under a single DB transaction.
            assign_offer(db,q,s,db.get(User,user['id']),'auto_assigned')
        elif accepted:
            note_org(db,s.shipper_id,'shipper',s.id,'offer_ready','Новое предложение',f'Поступило подтверждённое предложение по {s.ref}')
    return {'ok':True}

@app.post('/api/offers/{offer_id}/accept')
def accept_offer(offer_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper':forbidden()
    with db_write() as db:
        q=db.get(Offer,offer_id)
        if not q:raise HTTPException(404,detail='Предложение не найдено')
        s=db.get(Shipment,q.shipment_id)
        if s.shipper_id!=user['org_id']:forbidden()
        result=assign_offer(db,q,s,db.get(User,user['id']),'assigned')
    return result


@app.post('/api/offers/{offer_id}/withdraw')
def withdraw_offer(offer_id:int, request:Request, user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier': forbidden()
    with db_write() as db:
        q=db.get(Offer,offer_id)
        if not q or q.carrier_id != user['org_id']: raise HTTPException(404,detail='Предложение не найдено')
        s=db.get(Shipment,q.shipment_id)
        if q.status not in ('waiting_driver','driver_ok') or s.selected_offer_id == q.id:
            invalid('Назначенное или уже закрытое предложение отозвать нельзя')
        q.status='withdrawn'
        if s.status=='choice' and not db.scalar(select(Offer.id).where(
            Offer.shipment_id==s.id,Offer.id!=q.id,Offer.status=='driver_ok')):
            s.status='search'
        audit(db,db.get(User,user['id']),s,'offer_withdrawn',f'offer={q.id}')
        note_org(db,s.shipper_id,'shipper',s.id,'offer_withdrawn','Предложение отозвано',f'Предложение по {s.ref} отозвано')
        driver=db.get(Driver,q.driver_id)
        if driver.user_id: note_user(db,driver.user_id,s.id,'offer_withdrawn','Предложение отозвано',f'{s.ref}: диспетчер отозвал предложение')
    return {'ok':True}

@app.post('/api/offers/{offer_id}/reject')
def reject_offer(offer_id:int, request:Request, user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper': forbidden()
    with db_write() as db:
        q=db.get(Offer,offer_id)
        if not q: raise HTTPException(404,detail='Предложение не найдено')
        s=db.get(Shipment,q.shipment_id)
        if s.shipper_id!=user['org_id']: forbidden()
        if q.status not in ('waiting_driver','driver_ok') or s.selected_offer_id == q.id:
            invalid('Это предложение уже закрыто')
        q.status='rejected'
        if s.status=='choice' and not db.scalar(select(Offer.id).where(
            Offer.shipment_id==s.id,Offer.id!=q.id,Offer.status=='driver_ok')):
            s.status='search'
        audit(db,db.get(User,user['id']),s,'offer_rejected',f'offer={q.id}')
        note_org(db,q.carrier_id,'carrier',s.id,'offer_rejected','Предложение отклонено',f'Предложение по {s.ref} отклонено')
        driver=db.get(Driver,q.driver_id)
        if driver.user_id: note_user(db,driver.user_id,s.id,'offer_rejected','Предложение отклонено',f'{s.ref}: предложение закрыто')
    return {'ok':True}

@app.post('/api/orders/{ref}/cancel')
def cancel_order(ref:str, request:Request, user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper': forbidden()
    with db_write() as db:
        s=get_visible_shipment(db,ref,user)
        if s.status not in ('search','choice') or s.selected_offer_id is not None:
            invalid('Назначенный рейс требует отдельного согласования отмены')
        s.status='cancelled';s.search_launched=False
        for q in db.scalars(select(Offer).where(Offer.shipment_id==s.id,
                      Offer.status.in_(['waiting_driver','driver_ok']))):
            q.status='cancelled'
            note_org(db,q.carrier_id,'carrier',s.id,'order_cancelled','Заявка отменена',f'Заявка {s.ref} отменена')
            driver=db.get(Driver,q.driver_id)
            if driver.user_id: note_user(db,driver.user_id,s.id,'order_cancelled','Заявка отменена',f'{s.ref}: заявка отменена')
        audit(db,db.get(User,user['id']),s,'order_cancelled')
    return {'ok':True}

class PublishIn(BaseModel):
    public_external: bool = False

@app.post('/api/orders/{ref}/search')
def publish_search(ref:str,payload:PublishIn,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper':forbidden()
    with db_write() as db:
        s=get_visible_shipment(db,ref,user)
        if s.status!='search' or s.search_launched:invalid('Поиск уже запущен или заявка назначена')
        if s.hazardous:invalid('Публикация поиска опасного или неподтверждённого груза в пилоте заблокирована')
        if s.pickup_date < business_today():invalid('Дата погрузки уже прошла')
        s.search_launched=True
        s.public_external=payload.public_external and not s.hazardous
        carriers={a.carrier_id for a in db.scalars(select(Approval).where(Approval.shipper_id==s.shipper_id,Approval.active.is_(True)))}
        if s.public_external:
            carriers.update(o.id for o in db.scalars(select(Organization).where(Organization.kind=='carrier')) if fleet_suitable(db,s,o.id))
        notified=0
        for carrier_id in carriers:
            if not fleet_suitable(db,s,carrier_id):continue
            for u in db.scalars(select(User).where(User.org_id==carrier_id,User.role=='carrier',User.active.is_(True))):
                note_user(db,u.id,s.id,'new_order','Новая заявка',f'Новый рейс: {s.from_city} → {s.to_city}, {float(s.tons):g} т')
                notified+=1
        audit(db,db.get(User,user['id']),s,'search_launched',f'notified={notified};public={s.public_external}')
    return {'ok':True,'notified':notified,'publicExternal':s.public_external}

@app.post('/api/shipper/carriers/{carrier_id}/approve')
def approve_carrier(carrier_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper':forbidden()
    with db_write() as db:
        c=db.get(Organization,carrier_id)
        if not c or c.kind!='carrier':raise HTTPException(404,detail='Перевозчик не найден')
        a=db.scalar(select(Approval).where(Approval.shipper_id==user['org_id'],Approval.carrier_id==carrier_id))
        if not a:db.add(Approval(shipper_id=user['org_id'],carrier_id=carrier_id,active=True))
        else:a.active=True
        note_org(db,carrier_id,'carrier',None,'approved','Доступ заказчика','Заказчик открыл полный доступ к заявкам')
    return {'ok':True}


@app.post('/api/shipper/carriers/{carrier_id}/revoke')
def revoke_carrier(carrier_id:int, request:Request, user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper':forbidden()
    with db_write() as db:
        approval=db.scalar(select(Approval).where(
            Approval.shipper_id==user['org_id'],Approval.carrier_id==carrier_id,
            Approval.active.is_(True)))
        if not approval:raise HTTPException(404,detail='Действующий допуск не найден')
        approval.active=False
        # Cancel pending bids, not previously assigned commitments.
        bids=db.scalars(select(Offer).join(Shipment,Shipment.id==Offer.shipment_id).where(
            Shipment.shipper_id==user['org_id'],Offer.carrier_id==carrier_id,
            Offer.status.in_(['waiting_driver','driver_ok']))).all()
        for q in bids:
            q.status='rejected'
            s=db.get(Shipment,q.shipment_id)
            if s.status=='choice' and not db.scalar(select(Offer.id).where(
                Offer.shipment_id==s.id,Offer.id!=q.id,Offer.status=='driver_ok')):
                s.status='search'
            audit(db,db.get(User,user['id']),s,'carrier_access_revoked',f'carrier={carrier_id};offer={q.id}')
            d=db.get(Driver,q.driver_id)
            if d.user_id:note_user(db,d.user_id,s.id,'offer_rejected','Допуск отозван',f'{s.ref}: предложение закрыто')
        note_org(db,carrier_id,'carrier',None,'access_revoked','Доступ закрыт',
                 'Заказчик закрыл доступ к новым договорным заявкам. Ранее назначенные рейсы сохранены.')
    return {'ok':True,'closedPendingOffers':len(bids)}

class AddDriver(BaseModel):
    name: str = Field(min_length=2,max_length=100)
    email: str = Field(min_length=6,max_length=180)
    password: str = Field(min_length=12,max_length=128)

@app.post('/api/carrier/drivers',status_code=201)
def add_driver(payload:AddDriver,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier' or not user['is_admin']:forbidden()
    email=normalize_email(payload.email)
    clean_name=clean_label(payload.name,2)
    with db_write() as db:
        if db.scalar(select(User.id).where(User.email==email)):invalid('Пользователь уже зарегистрирован')
        u=User(org_id=user['org_id'],name=clean_name,role='driver',email=email,password_hash=pass_hash(payload.password),is_admin=False)
        db.add(u);db.flush()
        d=Driver(org_id=user['org_id'],user_id=u.id,name=u.name)
        db.add(d);db.flush()
        return {'id':d.id,'name':d.name}

@app.get('/api/tariff-suggestion')
def tariff_suggestion(from_city:str,to_city:str,pickup_address:str='',delivery_address:str='',product:str='',vehicle_type:str='',tons:float=0,user=Depends(user_session)):
    if user['role']!='shipper':forbidden()
    if not math.isfinite(tons) or tons<=0 or tons>100:invalid('Неверный объём',422)
    if not delivery_address.strip():return {'found':False,'budget':0,'rate':0,'source':'none','minObservedRate':None}
    with SessionLocal() as db:
        history=db.scalars(select(Shipment).where(Shipment.shipper_id==user['org_id'],Shipment.from_city==from_city.strip(),
                       Shipment.to_city==to_city.strip(),Shipment.pickup_address==pickup_address.strip(),Shipment.delivery_address==delivery_address.strip(),Shipment.product==product.strip(),Shipment.vehicle_kind==vehicle_type,
                       Shipment.status=='completed').order_by(Shipment.unload_date.desc(),Shipment.id.desc())).all()
        if not history:return {'found':False,'budget':0,'rate':0,'source':'none','minObservedRate':None}
        last=history[0]
        q=db.get(Offer,last.selected_offer_id) if last.selected_offer_id else None
        if not q:return {'found':False,'budget':0,'rate':0,'source':'none','minObservedRate':None}
        rate=round(q.price/float(last.tons))
        minimum=min(round(db.get(Offer,h.selected_offer_id).price/float(h.tons)) for h in history if h.selected_offer_id)
        return {'found':True,'budget':round(rate*tons),'rate':rate,'source':'last_completed','minObservedRate':minimum,
                'lastOrderRef':last.ref,'lastDate':last.unload_date.isoformat()}

@app.post('/api/orders/{ref}/complete')
def complete_order(ref:str,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper':forbidden()
    with db_write() as db:
        s=get_visible_shipment(db,ref,user)
        if s.status!='confirmed' or s.hazardous:invalid('Завершение доступно для подтверждённых тестовых перевозок')
        s.status='completed'
        audit(db,db.get(User,user['id']),s,'completed')
    return {'ok':True}

@app.get('/api/audit/{ref}')
def audit_log(ref:str,user=Depends(user_session)):
    if user['role']!='shipper':forbidden()
    with SessionLocal() as db:
        s=get_visible_shipment(db,ref,user)
        entries=db.scalars(select(Audit).where(Audit.shipment_id==s.id).order_by(Audit.id)).all()
        return [{'at':a.at.isoformat(),'action':a.action,'actor':a.actor_id,'details':a.details} for a in entries]

def csv_safe(value):
    s=str(value if value is not None else '')
    return "'"+s if s and s.lstrip().startswith(('=','+','-','@')) else s

@app.get('/api/export.csv')
def export_csv(user=Depends(user_session)):
    if user['role']!='shipper':forbidden()
    with SessionLocal() as db:
        ships=db.scalars(select(Shipment).where(Shipment.shipper_id==user['org_id']).order_by(Shipment.pickup_date)).all()
        output=io.StringIO();w=csv.writer(output,delimiter=';')
        w.writerow(['Номер','Статус','Менеджер','Дата погрузки','Время','Дата выгрузки','Грузополучатель','Продукт','Объем, т','ЭПД','ЭТрН','Тип ТС','Способ','Перевозчик','Водитель','Откуда','Куда','Машина','Тариф, руб/т','Стоимость, руб','Адрес доставки'])
        for s in ships:
            d=shipment_dict(s,db)
            w.writerow([csv_safe(d.get(k,'')) for k in ['id','status','manager','date','time','unload','recipient','product','tons','epd','etrn','vehicleType','mode','carrier','driver','from','to','car','rate','price','deliveryAddress']])
        data='\ufeff'+output.getvalue()
    return Response(data,media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="logistik_orders.csv"','Cache-Control':'no-store'})

def normalize_email(value: str) -> str:
    value = value.strip()
    if value.lower().startswith('mailto:'): value = value[7:]
    value = value.lower()
    local, sep, domain = value.rpartition('@')
    if (not sep or len(value)>180 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or local.startswith('.') or local.endswith('.') or '..' in local
            or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain)):
        invalid('Укажите почту без пробелов и лишних символов', 422)
    return value

def clean_label(value: str, minimum: int = 2) -> str:
    value = value.strip()
    if len(value)<minimum or any(ord(c)<32 for c in value): invalid('Проверьте название или имя', 422)
    return value

def same_origin(request: Request):
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'): forbidden('Недопустимый источник запроса')

def consume_action(key: str, limit: int, minutes: int = 60):
    """Commit the durable throttle even if the subsequent operation is rejected."""
    now=datetime.now(timezone.utc); denied=False; key=sha_token(key)
    with db_write() as db:
        bucket=db.get(LoginThrottle,key)
        if bucket and bucket.expires.replace(tzinfo=timezone.utc)<=now:
            db.delete(bucket);db.flush();bucket=None
        if bucket and bucket.failures>=limit: denied=True
        elif bucket: bucket.failures+=1
        else: db.add(LoginThrottle(key=key,failures=1,expires=now+timedelta(minutes=minutes)))
    if denied: raise HTTPException(429,detail='Слишком много запросов. Повторите позже')

class Signup(BaseModel):
    role: Literal['shipper','carrier','driver']
    name: str = Field(min_length=2,max_length=100)
    email: str = Field(min_length=6,max_length=180)
    password: str = Field(min_length=12,max_length=128)
    organization: str = Field(default='',max_length=160)
    invitation_token: str | None = Field(default=None,min_length=32,max_length=200)
    vehicle_kind: Literal['Автоцистерна','Фура'] | None = None
    vehicle_label: str = Field(default='',max_length=100)
    capacity_tons: Decimal | None = Field(default=None,gt=0,le=100,max_digits=7,decimal_places=2)

def check_invitation(inv: ParticipantInvitation | None):
    if not inv: raise HTTPException(404,detail='Приглашение не найдено')
    if inv.status!='pending': invalid('Приглашение уже обработано')
    if inv.expires.replace(tzinfo=timezone.utc)<=datetime.now(timezone.utc): invalid('Срок приглашения истёк. Попросите новую ссылку')

def activate_partner(db: Session, shipper_id: int, carrier_id: int):
    approval=db.scalar(select(Approval).where(Approval.shipper_id==shipper_id,Approval.carrier_id==carrier_id))
    if approval: approval.active=True
    else: db.add(Approval(shipper_id=shipper_id,carrier_id=carrier_id,active=True))
    for org_id,role in ((shipper_id,'shipper'),(carrier_id,'carrier')):
        note_org(db,org_id,role,None,'partner_connected','Партнёр подключён','Обе стороны подтвердили подключение заказчика и перевозчика')

def attach_team(db: Session, target: User, org_id: int):
    if target.org_id==org_id: return
    old_org=db.get(Organization,target.org_id)
    if target.role!='driver' or old_org.kind!='driver': invalid('Этот участник уже состоит в другой компании')
    driver=db.scalar(select(Driver).where(Driver.user_id==target.id))
    if not driver: invalid('Профиль водителя не найден')
    if db.scalar(select(Offer.id).where(Offer.driver_id==driver.id).limit(1)):
        invalid('Водителя с историей рейсов нельзя переводить автоматически')
    driver.org_id=org_id;target.org_id=org_id;target.is_admin=False
    # A second device must refresh the changed membership before continuing.
    db.execute(delete(WebSession).where(WebSession.user_id==target.id))

def accept_invitation(db: Session, inv: ParticipantInvitation, recipient: User):
    check_invitation(inv)
    sender=db.get(User,inv.sender_user_id)
    if not sender or not sender.active or sender.org_id!=inv.sender_org_id:
        invalid('Автор приглашения больше не имеет доступа к компании')
    if recipient.role!=inv.target_role or recipient.email!=inv.target_email: forbidden('Приглашение предназначено другому участнику')
    if inv.kind=='partner':
        if {sender.role,recipient.role}!={'shipper','carrier'}: invalid('Несовместимые роли партнёров')
        activate_partner(db,sender.org_id if sender.role=='shipper' else recipient.org_id,
                         sender.org_id if sender.role=='carrier' else recipient.org_id)
    elif inv.kind=='team':
        if not sender.is_admin: forbidden('Автор больше не управляет сотрудниками')
        attach_team(db,recipient,sender.org_id)
    elif inv.kind=='driver_join':
        if recipient.role!='carrier' or not recipient.is_admin: forbidden('Подключить водителя может руководитель перевозчика')
        attach_team(db,sender,recipient.org_id)
    else: invalid('Неизвестный вид приглашения')
    inv.status='accepted';inv.target_user_id=recipient.id;inv.responded_at=datetime.now(timezone.utc)
    note_user(db,sender.id,None,'invitation_accepted','Приглашение принято','Участник подтвердил подключение')
    note_user(db,recipient.id,None,'invitation_accepted','Подключение подтверждено','Список участников обновлён')

@app.get('/api/config')
def public_config():
    return {'version':VERSION,'signupEnabled':os.getenv('LOGISTIK_ALLOW_SIGNUP','0')=='1',
            'roles':['shipper','carrier','driver'],'emailDelivery':False}

@app.post('/api/signup',status_code=201)
def register_account(payload:Signup,request:Request,response:Response):
    if os.getenv('LOGISTIK_ALLOW_SIGNUP','0')!='1': forbidden('Самостоятельная регистрация пока не открыта')
    same_origin(request)
    email=normalize_email(payload.email);name=clean_label(payload.name)
    if not payload.password.strip(): invalid('Пароль не может состоять только из пробелов',422)
    consume_action('signup:'+(request.client.host if request.client else 'unknown'),30)
    token,csrf=secrets.token_urlsafe(40),secrets.token_urlsafe(28)
    with db_write() as db:
        if db.scalar(select(User.id).where(User.email==email)): invalid('Этот адрес уже зарегистрирован. Войдите в свой кабинет')
        inv=None
        if payload.invitation_token:
            inv=db.scalar(select(ParticipantInvitation).where(ParticipantInvitation.token_hash==sha_token(payload.invitation_token)))
            check_invitation(inv)
            if inv.target_user_id is not None: invalid('Участник уже зарегистрирован. Войдите для принятия приглашения')
            if inv.target_email!=email or inv.target_role!=payload.role: forbidden('Почта или роль не совпадает с приглашением')
        if inv and inv.kind=='team':
            org=db.get(Organization,inv.sender_org_id);admin=False
        else:
            organization=clean_label(payload.organization,3) if payload.role!='driver' else name
            org=Organization(name=organization,kind=payload.role);db.add(org);db.flush();admin=payload.role!='driver'
        account=User(org_id=org.id,email=email,name=name,role=payload.role,password_hash=pass_hash(payload.password),is_admin=admin)
        db.add(account);db.flush()
        if payload.role=='driver': db.add(Driver(org_id=org.id,user_id=account.id,name=name))
        fields=[payload.vehicle_kind is not None,bool(payload.vehicle_label),payload.capacity_tons is not None]
        if any(fields):
            if payload.role!='carrier' or not admin or not all(fields): invalid('Для машины заполните тип, обозначение и грузоподъёмность',422)
            db.add(Vehicle(org_id=org.id,label=clean_label(payload.vehicle_label,3),kind=payload.vehicle_kind,capacity=payload.capacity_tons))
        if inv: accept_invitation(db,inv,account)
        db.add(WebSession(token_hash=sha_token(token),user_id=account.id,csrf=csrf,expires=datetime.now(timezone.utc)+timedelta(days=7)))
    response.set_cookie('logistik_session',token,httponly=True,samesite='strict',
                        secure=os.getenv('LOGISTIK_SECURE_COOKIE','0')=='1',max_age=7*86400,path='/')
    return {'ok':True,'message':'Кабинет создан. Подключите участников в разделе «Участники».'}

class InviteIn(BaseModel):
    kind: Literal['partner','team','driver_join']
    role: Literal['shipper','carrier','driver']
    email: str = Field(min_length=6,max_length=180)

class InvitationDecision(BaseModel):
    accepted: bool
    token: str | None = Field(default=None,min_length=32,max_length=200)

def invitation_dict(db: Session, inv: ParticipantInvitation, outgoing: bool):
    sender=db.get(User,inv.sender_user_id);org=db.get(Organization,inv.sender_org_id)
    status=inv.status
    if status=='pending' and inv.expires.replace(tzinfo=timezone.utc)<=datetime.now(timezone.utc): status='expired'
    return {'id':inv.id,'kind':inv.kind,'role':inv.target_role,'email':inv.target_email,
            'status':status,'outgoing':outgoing,'organization':org.name,'sender':sender.name,
            'expires':inv.expires.isoformat(),'respondedAt':inv.responded_at.isoformat() if inv.responded_at else None}

@app.get('/api/participants')
def list_participants(user=Depends(user_session)):
    with SessionLocal() as db:
        partners=[]
        if user['role'] in ('shipper','carrier'):
            clauses=[Approval.active.is_(True),Approval.shipper_id==user['org_id']] if user['role']=='shipper' else [Approval.active.is_(True),Approval.carrier_id==user['org_id']]
            for a in db.scalars(select(Approval).where(*clauses)):
                org=db.get(Organization,a.carrier_id if user['role']=='shipper' else a.shipper_id)
                partners.append({'id':org.id,'name':org.name,'role':org.kind})
        team=[]
        if user['role']!='driver':
            team=[{'id':u.id,'name':u.name,'email':u.email,'role':u.role,'active':u.active,'admin':u.is_admin}
                  for u in db.scalars(select(User).where(User.org_id==user['org_id']))]
        invitations=[]
        for inv in db.scalars(select(ParticipantInvitation).where(
                (ParticipantInvitation.sender_user_id==user['id']) | (ParticipantInvitation.target_user_id==user['id'])).order_by(ParticipantInvitation.id.desc()).limit(100)):
            invitations.append(invitation_dict(db,inv,inv.sender_user_id==user['id']))
        org=db.get(Organization,user['org_id'])
        return {'partners':partners,'team':team,'invitations':invitations,'organization':org.name,
                'standaloneDriver':user['role']=='driver' and org.kind=='driver'}

@app.post('/api/participants/invitations',status_code=201)
def invite_participant(payload:InviteIn,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf']);email=normalize_email(payload.email)
    if payload.kind=='partner':
        if user['role'] not in ('shipper','carrier') or payload.role!=('carrier' if user['role']=='shipper' else 'shipper'): forbidden('Выберите противоположную роль партнёра')
    elif payload.kind=='team':
        if not user['is_admin'] or user['role'] not in ('shipper','carrier') or payload.role not in ([user['role'],'driver'] if user['role']=='carrier' else ['shipper']): forbidden('Нет прав добавлять такого сотрудника')
    elif payload.kind=='driver_join':
        if user['role']!='driver' or payload.role!='carrier': forbidden()
    if email==user['email']: invalid('Нельзя пригласить самого себя')
    consume_action('invite:'+str(user['id']),30)
    raw=secrets.token_urlsafe(40)
    with db_write() as db:
        target=db.scalar(select(User).where(User.email==email))
        if target and (not target.active or target.role!=payload.role): invalid('Для этого адреса выбрана неподходящая роль или доступ закрыт')
        if target and target.org_id==user['org_id']: invalid('Участник уже состоит в вашей компании')
        if payload.kind=='team' and target:
            if target.role!='driver' or db.get(Organization,target.org_id).kind!='driver': invalid('Участник уже состоит в другой компании')
        if payload.kind=='driver_join':
            if db.get(Organization,user['org_id']).kind!='driver': invalid('Вы уже подключены к перевозчику')
            if target and not target.is_admin: invalid('Укажите почту руководителя перевозчика')
        pending=db.scalar(select(ParticipantInvitation).where(ParticipantInvitation.sender_user_id==user['id'],
            ParticipantInvitation.target_email==email,ParticipantInvitation.kind==payload.kind,ParticipantInvitation.status=='pending',
            ParticipantInvitation.expires>datetime.now(timezone.utc)))
        if pending: invalid('Приглашение уже есть. Используйте «Обновить ссылку»')
        inv=ParticipantInvitation(sender_user_id=user['id'],sender_org_id=user['org_id'],kind=payload.kind,
            target_email=email,target_role=payload.role,target_user_id=target.id if target else None,
            token_hash=sha_token(raw),expires=datetime.now(timezone.utc)+timedelta(days=7))
        db.add(inv);db.flush()
        if target: note_user(db,target.id,None,'participant_invitation','Приглашение к подключению','Откройте «Участники» и подтвердите или отклоните приглашение')
        result={'id':inv.id,'link':str(request.base_url).rstrip('/')+'/#invite='+raw,'emailSent':False}
    return result

@app.post('/api/participants/invitations/{invitation_id}/respond')
def respond_invitation(invitation_id:int,payload:InvitationDecision,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        inv=db.get(ParticipantInvitation,invitation_id);check_invitation(inv)
        # Email alone cannot claim an invitation created before the account existed.
        if inv.target_user_id!=user['id']:
            if inv.target_user_id is not None or not payload.token or not hmac.compare_digest(inv.token_hash,sha_token(payload.token)):
                forbidden('Откройте персональную ссылку приглашения')
        recipient=db.get(User,user['id'])
        if recipient.email!=inv.target_email or recipient.role!=inv.target_role: forbidden('Это приглашение другому участнику')
        if payload.accepted: accept_invitation(db,inv,recipient)
        else: inv.status='rejected';inv.target_user_id=recipient.id;inv.responded_at=datetime.now(timezone.utc)
    return {'ok':True,'relogin':payload.accepted and user['role']=='driver'}

@app.post('/api/participants/invitations/{invitation_id}/cancel')
def cancel_invitation(invitation_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        inv=db.get(ParticipantInvitation,invitation_id)
        if not inv or inv.sender_user_id!=user['id']: raise HTTPException(404,detail='Приглашение не найдено')
        check_invitation(inv);inv.status='cancelled';inv.responded_at=datetime.now(timezone.utc)
    return {'ok':True}

@app.post('/api/participants/invitations/{invitation_id}/renew')
def renew_invitation(invitation_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf']);consume_action('invite:'+str(user['id']),30)
    raw=secrets.token_urlsafe(40)
    with db_write() as db:
        inv=db.get(ParticipantInvitation,invitation_id)
        if not inv or inv.sender_user_id!=user['id']: raise HTTPException(404,detail='Приглашение не найдено')
        if inv.status!='pending': invalid('Приглашение уже обработано')
        inv.token_hash=sha_token(raw);inv.expires=datetime.now(timezone.utc)+timedelta(days=7)
    return {'link':str(request.base_url).rstrip('/')+'/#invite='+raw,'emailSent':False}

class ResolveInvitation(BaseModel):
    token: str = Field(min_length=32,max_length=200)

@app.post('/api/participants/invitation-info')
def resolve_invitation(payload:ResolveInvitation,request:Request):
    same_origin(request)
    consume_action('invite-info:'+(request.client.host if request.client else 'unknown'),120,5)
    with SessionLocal() as db:
        inv=db.scalar(select(ParticipantInvitation).where(ParticipantInvitation.token_hash==sha_token(payload.token)))
        check_invitation(inv)
        return {**invitation_dict(db,inv,False),'registered':inv.target_user_id is not None}

class AddVehicle(BaseModel):
    label: str = Field(min_length=3,max_length=100)
    kind: Literal['Автоцистерна','Фура']
    capacity: Decimal = Field(gt=0,le=100,max_digits=7,decimal_places=2)

@app.post('/api/carrier/vehicles',status_code=201)
def add_vehicle(payload:AddVehicle,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier' or not user['is_admin']: forbidden()
    label=clean_label(payload.label,3)
    with db_write() as db:
        if any(existing.casefold()==label.casefold() for existing in db.scalars(select(Vehicle.label).where(Vehicle.org_id==user['org_id']))):
            invalid('Машина с таким обозначением уже добавлена')
        vehicle=Vehicle(org_id=user['org_id'],label=label,kind=payload.kind,capacity=payload.capacity)
        db.add(vehicle);db.flush();return {'id':vehicle.id,'label':label}

@app.post('/api/participants/team/{member_id}/deactivate')
def deactivate_member(member_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if not user['is_admin'] or user['role'] not in ('shipper','carrier'): forbidden()
    with db_write() as db:
        member=db.get(User,member_id)
        if not member or member.org_id!=user['org_id']: raise HTTPException(404,detail='Сотрудник не найден')
        if member.id==user['id'] or member.is_admin: invalid('Доступ руководителя нельзя отключить здесь')
        driver=db.scalar(select(Driver).where(Driver.user_id==member.id))
        if driver and db.scalar(select(Offer.id).join(Shipment,Shipment.id==Offer.shipment_id).where(Offer.driver_id==driver.id,
                Offer.status.in_(['waiting_driver','driver_ok','selected']),Shipment.status.notin_(['completed','cancelled']))):
            invalid('Сначала завершите или отмените действующие предложения и рейсы водителя')
        member.active=False;db.execute(delete(WebSession).where(WebSession.user_id==member.id))
    return {'ok':True}

# Test data exclusively. No private contents of the customer's spreadsheet are imported.
def seed_demo():
    if not DEMO:return
    with db_write() as db:
        if db.scalar(select(User.id).limit(1)) is not None:return
        ship=Organization(name='Демо: Грузоотправитель',kind='shipper')
        c1=Organization(name='Демо: Каскад Транс',kind='carrier')
        c2=Organization(name='Демо: Транзит-Урал',kind='carrier')
        db.add_all([ship,c1,c2]);db.flush()
        password='Demo-2026-Start!'
        u1=User(org_id=ship.id,email='shipper@example.test',name='Логист',role='shipper',is_admin=True,password_hash=pass_hash(password))
        u2=User(org_id=c1.id,email='carrier@example.test',name='Диспетчер',role='carrier',is_admin=True,password_hash=pass_hash(password))
        u3=User(org_id=c1.id,email='driver@example.test',name='Сергей К.',role='driver',password_hash=pass_hash(password))
        u4=User(org_id=c2.id,email='carrier2@example.test',name='Диспетчер 2',role='carrier',is_admin=True,password_hash=pass_hash(password))
        u5=User(org_id=c2.id,email='driver2@example.test',name='Андрей М.',role='driver',password_hash=pass_hash(password))
        db.add_all([u1,u2,u3,u4,u5]);db.flush()
        db.add_all([Approval(shipper_id=ship.id,carrier_id=c1.id),Approval(shipper_id=ship.id,carrier_id=c2.id)])
        d1=Driver(org_id=c1.id,user_id=u3.id,name='Сергей К.')
        d2=Driver(org_id=c2.id,user_id=u5.id,name='Андрей М.')
        v1=Vehicle(org_id=c1.id,label='Демо А123АА / АВ4589',kind='Автоцистерна',capacity=25)
        v2=Vehicle(org_id=c2.id,label='Демо В517ВК / ЕР8392',kind='Автоцистерна',capacity=25)
        db.add_all([d1,d2,v1,v2]);db.flush()
        day=date.today()+timedelta(days=1)
        for idx, (destination,product,hazardous,budget) in enumerate([
            ('Самара','Изопропиловый спирт',True,150000),('Уфа','Вода техническая',False,85000),
            ('Казань','Вода техническая',False,110000),('Москва','Изопропиловый спирт',True,250000)]):
            s=Shipment(ref=f'P-{1048+idx}',shipper_id=ship.id,from_city='Орск',to_city=destination,
                recipient=f'Демо грузополучатель {idx+1}',product=product,tons=24,pickup_date=day+timedelta(days=idx),
                pickup_time='09:00',unload_date=day+timedelta(days=idx+1),vehicle_kind='Автоцистерна',
                mode='Доставка',budget=budget,rate=0,manager='Демо-логист',hazardous=hazardous,note='Тестовые сведения',epd='',etrn='')
            db.add(s);db.flush();audit(db,u1,s,'created','Демонстрационная заявка')
seed_demo()
