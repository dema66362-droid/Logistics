"""Логистик: минимальный серверный пилот (не юридическая система ЭПД)."""
from __future__ import annotations
import csv
import asyncio
import base64
import json
import hashlib
import hmac
import io
import math
import os
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager, asynccontextmanager
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
                        inspect, select, text, update)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.exc import SQLAlchemyError

BASE = Path(__file__).resolve().parent
DB_URL = os.environ.get('LOGISTIK_DATABASE_URL', f'sqlite:///{BASE / "data" / "logistik.sqlite3"}')
if DB_URL.startswith('postgres://'):
    DB_URL = 'postgresql://' + DB_URL[len('postgres://'):]
# SQLAlchemy 2.1 defaults to psycopg 3; this release installs psycopg2.
if DB_URL.startswith('postgresql://'):
    DB_URL = 'postgresql+psycopg2://' + DB_URL[len('postgresql://'):]
VERSION = '3.2.0'
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
    inn: Mapped[str | None] = mapped_column(String(12))

    directory_key: Mapped[str] = mapped_column(String(180),default='')
    disposition: Mapped[str] = mapped_column(String(12),default='active')

@event.listens_for(Organization,'before_insert')
@event.listens_for(Organization,'before_update')
def index_company_name(mapper,connection,org):
    org.directory_key=org.name.casefold().replace('ё','е')

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

    surname: Mapped[str] = mapped_column(String(100),default='')

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
    sender_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    recipient_archived: Mapped[bool] = mapped_column(Boolean, default=False)

class ShipperDriver(Base):
    __tablename__ = 'shipper_drivers'
    id: Mapped[int] = mapped_column(primary_key=True)
    shipper_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint('shipper_id','user_id'),)

class ParticipantComment(Base):
    __tablename__ = 'participant_comments'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    subject: Mapped[str] = mapped_column(String(80))
    body: Mapped[str] = mapped_column(Text, default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint('user_id','subject'),)

class RecoveryCode(Base):
    __tablename__ = 'recovery_codes'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'),index=True)
    code_hash: Mapped[str] = mapped_column(String(64),unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)

class PushSubscription(Base):
    __tablename__ = 'push_subscriptions'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'),index=True)
    endpoint_hash: Mapped[str] = mapped_column(String(64),unique=True)
    endpoint: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(String(180))
    auth: Mapped[str] = mapped_column(String(100))
    session_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean,default=True)

class PushDelivery(Base):
    __tablename__ = 'push_deliveries'
    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey('notifications.id'))
    subscription_id: Mapped[int] = mapped_column(ForeignKey('push_subscriptions.id'))
    status: Mapped[str] = mapped_column(String(20),default='pending')
    attempts: Mapped[int] = mapped_column(Integer,default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint('notification_id','subscription_id'),)

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

class DispatchRecipient(Base):
    __tablename__ = 'dispatch_recipients'
    shipment_id: Mapped[int] = mapped_column(ForeignKey('shipments.id'), primary_key=True)
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'), primary_key=True)
    declined: Mapped[bool] = mapped_column(Boolean, default=False)
    review_seen: Mapped[bool] = mapped_column(Boolean, default=False)

class RosterMember(Base):
    __tablename__ = 'carrier_roster'
    id: Mapped[int] = mapped_column(primary_key=True)
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    driver_id: Mapped[int] = mapped_column(ForeignKey('drivers.id'))
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(180), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    share_busy: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint('carrier_id','driver_id'), UniqueConstraint('carrier_id','email'))

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
    # Original request is immutable. These nullable overrides keep historical assignments intact.
    slot_day: Mapped[date | None] = mapped_column(Date)
    slot_time: Mapped[str | None] = mapped_column(String(5))
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
    pickup_company: Mapped[str] = mapped_column(String(160),default='')
    pickup_contact: Mapped[str] = mapped_column(String(200),default='')
    pickup_address: Mapped[str] = mapped_column(String(300), default='')
    delivery_address: Mapped[str] = mapped_column(String(300), default='')
    recipient_contact: Mapped[str] = mapped_column(String(200), default='')
    terms: Mapped[str] = mapped_column(Text, default='')
    terms_version: Mapped[int] = mapped_column(Integer, default=1)
    public_external: Mapped[bool] = mapped_column(Boolean, default=False)
    search_launched: Mapped[bool] = mapped_column(Boolean, default=False)
    tariff_source: Mapped[str] = mapped_column(String(30), default='manual')
    __table_args__ = (CheckConstraint('tons > 0'),)

    creator_id: Mapped[int | None] = mapped_column(Integer)
    creator_name: Mapped[str] = mapped_column(String(100),default='')
    first_price: Mapped[int | None] = mapped_column(Integer)
    price_revised: Mapped[bool] = mapped_column(Boolean,default=False)
    revision: Mapped[int] = mapped_column(Integer,default=1)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class InternalTrip(Base):
    __tablename__='internal_trips'
    shipment_id: Mapped[int] = mapped_column(ForeignKey('shipments.id'),primary_key=True)
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    customer: Mapped[str] = mapped_column(String(160))
    driver_id: Mapped[int] = mapped_column(ForeignKey('drivers.id'))
    vehicle_id: Mapped[int] = mapped_column(ForeignKey('vehicles.id'))
    status: Mapped[str] = mapped_column(String(30))
    request_id: Mapped[str] = mapped_column(String(72))
    signature: Mapped[str] = mapped_column(String(64))
    __table_args__=(UniqueConstraint('carrier_id','request_id'),)

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
    delivery_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='waiting_driver')
    terms_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted_terms_version: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint('price > 0'),)

class SlotProposal(Base):
    __tablename__ = 'slot_proposals'
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey('shipments.id'), index=True)
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    offer_id: Mapped[int | None] = mapped_column(ForeignKey('offers.id'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    day: Mapped[date] = mapped_column(Date)
    time: Mapped[str] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(String(16), default='pending')

    new_unload: Mapped[date | None] = mapped_column(Date)

class InnRestriction(Base):
    __tablename__='inn_restrictions'
    inn: Mapped[str] = mapped_column(String(12),primary_key=True)
    state: Mapped[str] = mapped_column(String(12))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class AdminSession(Base):
    __tablename__='site_admin_sessions'
    token_hash: Mapped[str] = mapped_column(String(64),primary_key=True)
    csrf: Mapped[str] = mapped_column(String(80))
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    credential_tag: Mapped[str] = mapped_column(String(64))

class FleetGrant(Base):
    __tablename__='fleet_grants'
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'),primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'),primary_key=True)

class DriverFleetGrant(Base):
    __tablename__='driver_fleet_grants'
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'),primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey('drivers.id'),primary_key=True)

class CarrierCosts(Base):
    __tablename__='carrier_costs'
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'),primary_key=True)
    empty_km: Mapped[Decimal] = mapped_column(Numeric(8,2))
    loaded_km: Mapped[Decimal] = mapped_column(Numeric(8,2))
    wait_day: Mapped[Decimal] = mapped_column(Numeric(10,2))

class CompanyPeer(Base):
    __tablename__='company_peers'
    first_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'),primary_key=True)
    second_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'),primary_key=True)

class VehiclePosition(Base):
    __tablename__='vehicle_positions'
    vehicle_id: Mapped[int] = mapped_column(ForeignKey('vehicles.id'),primary_key=True)
    city: Mapped[str] = mapped_column(String(120))
    home: Mapped[str] = mapped_column(String(120))
    available: Mapped[date] = mapped_column(Date)

class RoadDistance(Base):
    __tablename__='road_distances'
    carrier_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'),primary_key=True)
    origin: Mapped[str] = mapped_column(String(120),primary_key=True)
    destination: Mapped[str] = mapped_column(String(120),primary_key=True)
    km: Mapped[int] = mapped_column(Integer)

class DriverLeave(Base):
    __tablename__ = 'driver_leave'
    id: Mapped[int] = mapped_column(primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey('drivers.id'), index=True)
    start: Mapped[date] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)
    __table_args__ = (CheckConstraint('start <= "end"'), UniqueConstraint('driver_id','start','end'))

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
    event_key: Mapped[str | None] = mapped_column(String(100),nullable=True)
    __table_args__ = (UniqueConstraint('user_id','event_key'),)

def initialize_schema():
    """Idempotent additive pilot migration, serialized against other app workers."""
    with engine.begin() as conn:
        if IS_SQLITE:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        elif engine.dialect.name == 'postgresql':
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': PILOT_WRITE_LOCK})
        Base.metadata.create_all(conn)
        latest = conn.scalar(select(func.max(SchemaMigration.version)))
        if latest and latest > '0032':
            raise RuntimeError('Database schema is newer than this application release')
        for table, definitions in {
            'organizations': {'inn':'VARCHAR(12)','directory_key':"VARCHAR(180) NOT NULL DEFAULT ''",'disposition':"VARCHAR(12) NOT NULL DEFAULT 'active'"},
            'users': {'surname':"VARCHAR(100) NOT NULL DEFAULT ''"},
            'slot_proposals': {'new_unload':'DATE'},
            'shipments': {'creator_id':'INTEGER','creator_name':"VARCHAR(100) NOT NULL DEFAULT ''",'revision':'INTEGER NOT NULL DEFAULT 1','first_price':'INTEGER','price_revised':'BOOLEAN NOT NULL DEFAULT 0','delete_after':'TIMESTAMP WITH TIME ZONE','slot_day':'DATE','slot_time':'VARCHAR(5)','pickup_company':"VARCHAR(160) NOT NULL DEFAULT ''",'pickup_contact':"VARCHAR(200) NOT NULL DEFAULT ''",'pickup_address':"VARCHAR(300) NOT NULL DEFAULT ''",'delivery_address':"VARCHAR(300) NOT NULL DEFAULT ''",'recipient_contact':"VARCHAR(200) NOT NULL DEFAULT ''",'terms':"TEXT NOT NULL DEFAULT ''",'terms_version':'INTEGER NOT NULL DEFAULT 1','public_external':'BOOLEAN NOT NULL DEFAULT 0','search_launched':'BOOLEAN NOT NULL DEFAULT 0','tariff_source':"VARCHAR(30) NOT NULL DEFAULT 'manual'"},
            'offers': {'delivery_day':'DATE','terms_accepted':'BOOLEAN NOT NULL DEFAULT 0','accepted_terms_version':'INTEGER NOT NULL DEFAULT 0'},
            'participant_invitations': {'sender_archived':'BOOLEAN NOT NULL DEFAULT 0','recipient_archived':'BOOLEAN NOT NULL DEFAULT 0'},
            'dispatch_recipients': {'declined':'BOOLEAN NOT NULL DEFAULT 0','review_seen':'BOOLEAN NOT NULL DEFAULT 0'},
            'carrier_roster': {'share_busy':'BOOLEAN NOT NULL DEFAULT 0'},
            'notifications': {'event_key':'VARCHAR(100)'}
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
        conn.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_event ON notifications (user_id,event_key)')
        if not conn.scalar(select(SchemaMigration.version).where(SchemaMigration.version == '0029')):
            conn.execute(SchemaMigration.__table__.insert().values(version='0029', applied_at=datetime.now(timezone.utc)))
        if not conn.scalar(select(SchemaMigration.version).where(SchemaMigration.version == '0030')):
            conn.execute(SchemaMigration.__table__.insert().values(version='0030', applied_at=datetime.now(timezone.utc)))

        if not conn.scalar(select(SchemaMigration.version).where(SchemaMigration.version=='0031')):
            # Keep previously agreed arrival times. Open counteroffers acquire an explicit decision.
            conn.execute(text('''UPDATE shipments SET
                slot_day=(SELECT pickup_date FROM offers WHERE offers.id=shipments.selected_offer_id),
                slot_time=(SELECT pickup_time FROM offers WHERE offers.id=shipments.selected_offer_id)
                WHERE selected_offer_id IS NOT NULL AND slot_day IS NULL'''))
            conn.execute(text('''
                INSERT INTO slot_proposals (shipment_id,carrier_id,offer_id,actor_id,day,time,status)
                SELECT o.shipment_id,o.carrier_id,o.id,
                       (SELECT MIN(u.id) FROM users u WHERE u.org_id=o.carrier_id AND u.role='carrier'),
                       o.pickup_date,o.pickup_time,'pending'
                FROM offers o JOIN shipments s ON s.id=o.shipment_id
                WHERE o.status IN ('waiting_driver','driver_ok') AND s.status IN ('search','choice')
                  AND (o.pickup_date<>s.pickup_date OR o.pickup_time<>s.pickup_time)
                  AND EXISTS (SELECT 1 FROM users u WHERE u.org_id=o.carrier_id AND u.role='carrier')
            '''))
            conn.execute(SchemaMigration.__table__.insert().values(version='0031',applied_at=datetime.now(timezone.utc)))

        if not conn.scalar(select(SchemaMigration.version).where(SchemaMigration.version=='0032')):
            for oid,name in conn.execute(select(Organization.id,Organization.name)):
                conn.execute(update(Organization).where(Organization.id==oid).values(directory_key=name.casefold().replace('ё','е')))
            conn.execute(text("""UPDATE shipments SET creator_id=(SELECT a.actor_id FROM audit a
                WHERE a.shipment_id=shipments.id AND a.action IN ('created','imported','internal_created') ORDER BY a.id LIMIT 1)"""))
            conn.execute(text("""UPDATE shipments SET creator_name=COALESCE((SELECT u.name FROM users u WHERE u.id=shipments.creator_id),manager,''),
                first_price=COALESCE(NULLIF(budget,0),(SELECT price FROM offers WHERE id=shipments.selected_offer_id))"""))
            conn.execute(text("""UPDATE shipments SET price_revised=TRUE WHERE selected_offer_id IS NOT NULL
                AND first_price<>(SELECT price FROM offers WHERE id=shipments.selected_offer_id)"""))
            conn.execute(SchemaMigration.__table__.insert().values(version='0032',applied_at=datetime.now(timezone.utc)))
        conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_shipments_delete_after ON shipments(delete_after)')
        conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_org_directory_inn ON organizations(inn)')
        conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_notes_order_kind ON notifications(shipment_id,kind)')

initialize_schema()

@asynccontextmanager
async def lifespan(application):
    from push_delivery import delivery_loop
    task=asyncio.create_task(delivery_loop(sys.modules[__name__]))
    from board_orders import housekeeping, cleanup
    await asyncio.to_thread(cleanup)
    cleaner=asyncio.create_task(housekeeping())
    try: yield
    finally:
        task.cancel();cleaner.cancel()
        await asyncio.gather(task,cleaner,return_exceptions=True)

app = FastAPI(title='Логистик MVP', docs_url=None, redoc_url=None, openapi_url=None,lifespan=lifespan)

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
            db.info['event_id']=secrets.token_hex(16)
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
    shipment.revision=(shipment.revision or 0)+1
    db.add(Audit(org_id=actor.org_id, actor_id=actor.id, shipment_id=shipment.id, action=action, details=details))
    notify_shipment_action(db,actor,shipment,action)

def note_user(db:Session, user_id:int, shipment_id:int|None, kind:str, title:str, msg:str):
    if not user_id: return
    account=db.get(User,user_id)
    if not account or not account.active or db.get(Organization,account.org_id).disposition!='active':return
    key=f"{db.info.setdefault('event_id',secrets.token_hex(16))}:{shipment_id}" if shipment_id is not None else None
    existing=db.scalar(select(Notification).where(Notification.user_id==user_id,Notification.event_key==key)) if key else None
    if existing:
        existing.kind=kind;existing.title=title;existing.message=msg
        return
    note=Notification(user_id=user_id,shipment_id=shipment_id,kind=kind,title=title,message=msg,event_key=key)
    db.add(note);db.flush()
    for subscription in db.scalars(select(PushSubscription).where(PushSubscription.user_id==user_id,PushSubscription.active.is_(True))):
        db.add(PushDelivery(notification_id=note.id,subscription_id=subscription.id))

def notify_shipment_action(db:Session,actor:User,shipment:Shipment,action:str):
    labels={'order_updated':'Изменена заявка','crew_reserved':'Экипаж поставлен в график','created':'Создана заявка','imported':'Импортирована заявка','search_launched':'Начат поиск машины',
            'offer_created':'Добавлено предложение','driver_ok':'Водитель подтвердил доступность',
            'driver_declined':'Водитель отклонил предложение','assigned':'Назначен перевозчик',
            'auto_assigned':'Назначен перевозчик','offer_withdrawn':'Предложение отозвано',
            'offer_rejected':'Предложение отклонено','order_cancelled':'Заявка отменена',
            'slot_proposed':'Требуется согласование времени', 'slot_accepted':'Время подачи согласовано',
            'slot_withdrawn':'Участник принял время заказчика','slot_rejected':'Сохранено прежнее время подачи','slot_changed':'Заказчик изменил время подачи',
            'completed':'Рейс завершён','carrier_declined':'Перевозчик отклонил заявку','cargo_review_requested':'Заявка на проверку допуска','cargo_review_received':'Перевозчик получил запрос на проверку','carrier_access_revoked':'Изменён допуск перевозчика'}
    internal=db.get(InternalTrip,shipment.id)
    if internal:
        titles={'internal_created':'Рейс внесён в график','internal_accept':'Водитель подтвердил рейс','internal_decline':'Водитель отклонил рейс',
                'internal_cancel':'Диспетчер отменил рейс','internal_complete':'Рейс завершён'}
        title=titles.get(action,labels.get(action,'Обновлён внутренний рейс'))
        note_org(db,internal.carrier_id,'carrier',shipment.id,action,title,shipment.ref+' · '+title)
        person=db.get(Driver,internal.driver_id)
        if person.user_id:note_user(db,person.user_id,shipment.id,action,title,shipment.ref+' · '+title)
        return
    recipients={u.id for u in db.scalars(select(User).where(User.org_id==shipment.shipper_id,User.role=='shipper',User.active.is_(True)))}
    # Notify all parties to this order, including losing bidders, using no prices,
    # private addresses, contact information, or competitor identities.
    bids=db.scalars(select(Offer).where(Offer.shipment_id==shipment.id)).all()
    carriers={q.carrier_id for q in bids}
    for q in bids:
        driver=db.get(Driver,q.driver_id)
        account=db.get(User,driver.user_id) if driver and driver.user_id else None
        if account and account.active:recipients.add(account.id)
    targets=dispatch_targets(db, shipment.id)
    if targets:
        carriers.update(cid for cid in targets if allowed(db,shipment,cid))
    elif action=='search_launched':
        carriers.update(a.carrier_id for a in db.scalars(select(Approval).where(Approval.shipper_id==shipment.shipper_id,Approval.active.is_(True))))
        if shipment.public_external:
            carriers.update(o.id for o in db.scalars(select(Organization).where(Organization.kind=='carrier')) if fleet_suitable(db,shipment,o.id))
        carriers={cid for cid in carriers if fleet_suitable(db,shipment,cid)}
    else:
        # An unaddressed search also has an audience before the first bid.
        delivered=select(User.org_id).join(Notification,Notification.user_id==User.id).where(
            Notification.shipment_id==shipment.id,Notification.kind=='new_order',User.role=='carrier').distinct()
        carriers.update(cid for cid in db.scalars(delivered) if shipment.public_external or allowed(db,shipment,cid))
    for cid in carriers:
        recipients.update(u.id for u in db.scalars(select(User).where(User.org_id==cid,User.role=='carrier',User.active.is_(True))))
    recipients.add(actor.id)
    title=labels.get(action,'Заявка обновлена')
    for uid in recipients:
        note_user(db,uid,shipment.id,'new_order' if action=='search_launched' else 'assigned' if action=='auto_assigned' else action,title,f'{shipment.ref}: {title.lower()}. Откройте свой кабинет.')

def note_org(db:Session, org_id:int, role:str, shipment_id:int|None, kind:str, title:str, msg:str):
    for u in db.scalars(select(User).where(User.org_id==org_id,User.role==role,User.active.is_(True))):
        note_user(db,u.id,shipment_id,kind,title,msg)

def fleet_suitable(db:Session, shipment:Shipment, carrier_id:int)->bool:
    return db.scalar(select(Vehicle.id).where(Vehicle.org_id==carrier_id,Vehicle.kind==shipment.vehicle_kind,Vehicle.capacity>=shipment.tons).limit(1)) is not None

def dispatch_targets(db: Session, shipment_id: int) -> set[int]:
    return set(db.scalars(select(DispatchRecipient.carrier_id).where(DispatchRecipient.shipment_id==shipment_id)))

def can_receive(db: Session, shipment: Shipment, carrier_id: int) -> bool:
    targets = dispatch_targets(db, shipment.id)
    if targets:
        return carrier_id in targets and not db.get(DispatchRecipient,(shipment.id,carrier_id)).declined and allowed(db, shipment, carrier_id)
    return allowed(db, shipment, carrier_id) and fleet_suitable(db, shipment, carrier_id)

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
        if not user or not user.active or db.get(Organization,user.org_id).disposition!='active':
            raise HTTPException(401, detail='Доступ закрыт')
        return dict(id=user.id, org_id=user.org_id, role=user.role, name=user.name,
                    email=user.email, surname=user.surname, is_admin=user.is_admin, csrf=websession.csrf)

class CarrierSignup(BaseModel):
    inn: str = Field(default='',max_length=12)
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
            if not user or not user.active or not password_matches or db.get(Organization,user.org_id).disposition!='active':
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

def official_slot(s: Shipment, chosen: Offer | None = None):
    if s.slot_day is not None:
        return s.slot_day,s.slot_time
    if chosen is not None:
        return chosen.pickup_date,chosen.pickup_time
    return s.pickup_date,s.pickup_time

def shipment_dict(s: Shipment, db: Session):
    chosen=db.get(Offer,s.selected_offer_id) if s.selected_offer_id else None
    carrier=db.get(Organization,chosen.carrier_id) if chosen else None
    driver=db.get(Driver,chosen.driver_id) if chosen else None
    vehicle=db.get(Vehicle,chosen.vehicle_id) if chosen else None
    planned_unload = (chosen.delivery_day or max(chosen.pickup_date,s.unload_date)) if chosen else s.unload_date
    day,time=official_slot(s,chosen)
    return {'id':s.ref,'revision':s.revision,'author':s.creator_name or s.manager,'date':day.isoformat(),
            'time':time,'unload':planned_unload.isoformat(),
            'requestedDate':s.pickup_date.isoformat(),'requestedTime':s.pickup_time,'requestedUnload':s.unload_date.isoformat(),
            'shipperName':db.get(Organization,s.shipper_id).name,'pickupCompany':s.pickup_company,
            'pickupContact':s.pickup_contact,
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

def sanitize_for_role(item:dict, role:str, *, external:bool=False, assigned:bool=False, known_parties:bool=False):
    if external:
        # Explicit allowlist: no exact address, recipient, contact, manager, budget, shipper identity, or offers.
        keys=('id','date','time','unload','product','tons','from','to','vehicleType','mode','status','terms','termsVersion','hazardous')
        return {k:item[k] for k in keys}
    if role=='driver':
        item.update(budget=0,rate=0,price=0,manager='',epd='',etrn='',note='',terms='',recipientContact=item['recipientContact'] if assigned else '',
                    pickupAddress=item['pickupAddress'] if assigned else '',deliveryAddress=item['deliveryAddress'] if assigned else '',
                    recipient=item['recipient'] if (assigned or known_parties) else '',
                    pickupCompany=item['pickupCompany'] if (assigned or known_parties) else '',shipperName=item['shipperName'] if (assigned or known_parties) else '',pickupContact=item['pickupContact'] if assigned else '',tariffSource='',publicExternal=False)
    return item

def offer_dict(o: Offer, db: Session):
    return {'id':f'Q-{o.id}','orderId':db.get(Shipment,o.shipment_id).ref,
            'carrier':db.get(Organization,o.carrier_id).name,'carrierId':o.carrier_id,
            'driver':db.get(Driver,o.driver_id).name,'car':db.get(Vehicle,o.vehicle_id).label,
            'driverId':o.driver_id,'vehicleId':o.vehicle_id,'price':o.price,'pickup':o.pickup_time,
            'day':o.pickup_date.isoformat(),'endDate':o.end_date.isoformat(),
            'deliveryDay':(o.delivery_day or max(o.pickup_date,db.get(Shipment,o.shipment_id).unload_date)).isoformat(),
            'status':o.status,'termsAccepted':o.terms_accepted,'approved':allowed(db,db.get(Shipment,o.shipment_id),o.carrier_id),
            'duration':'время — по графику'}

@app.get('/api/state')
def get_state(user=Depends(user_session)):
    return dashboard_state(user)

def dashboard_state(user,period=None):
    with SessionLocal() as db:
        if user['role']=='shipper':
            shipments=db.scalars(select(Shipment).where(Shipment.shipper_id==user['org_id']).order_by(func.coalesce(Shipment.slot_day,Shipment.pickup_date),Shipment.id)).all()
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
                ((s.status in ('search','choice') and can_receive(db,s,user['org_id'])) or
                 (s.selected_offer_id and db.get(Offer,s.selected_offer_id).carrier_id==user['org_id']))]
            shipments += [s for s in public if s.id not in keys and public_visible(db,s,user['org_id'])]
            review_orders=db.scalars(select(Shipment).join(DispatchRecipient,DispatchRecipient.shipment_id==Shipment.id).where(
                DispatchRecipient.carrier_id==user['org_id'],DispatchRecipient.declined.is_(False),
                Shipment.shipper_id.in_(ships),Shipment.hazardous.is_(True),Shipment.search_launched.is_(False),Shipment.status=='search')).all()
            shipments=list({s.id:s for s in [*shipments,*review_orders]}.values())
            offers=db.scalars(select(Offer).where(Offer.carrier_id==user['org_id'])).all()
            drivers=carrier_drivers(db,user['org_id'])
            vehicles=db.scalars(select(Vehicle).where(Vehicle.org_id==user['org_id'])).all()
        elif user['role']=='driver':
            driver=db.scalar(select(Driver).where(Driver.user_id==user['id']))
            mine=select(Offer).where(Offer.driver_id==driver.id) if driver else None
            if driver and period:
                start,end=period
                mine=mine.join(Shipment,Shipment.id==Offer.shipment_id).where(
                    func.coalesce(Shipment.slot_day,Shipment.pickup_date)<=end,Offer.end_date>=start)
            offers=db.scalars(mine).all() if driver else []
            ids=[o.shipment_id for o in offers if o.status in ('waiting_driver','driver_ok','selected')]
            shipments=db.scalars(select(Shipment).where(Shipment.id.in_(ids))).all() if ids else []
            drivers=[driver] if driver else []
            vehicles=db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in offers]))).all() if offers else []
        else: forbidden()
        private={}
        if user['role'] in ('carrier','driver'):
            driver_ids=[d.id for d in drivers]
            clause=InternalTrip.carrier_id==user['org_id'] if user['role']=='carrier' else InternalTrip.driver_id.in_(driver_ids)
            manual_query=select(InternalTrip).where(clause)
            if period:
                manual_query=manual_query.join(Shipment,Shipment.id==InternalTrip.shipment_id).where(
                    Shipment.pickup_date<=period[1],Shipment.unload_date>=period[0])
            private={t.shipment_id:t for t in db.scalars(manual_query)}
            if private:shipments.extend(db.scalars(select(Shipment).where(Shipment.id.in_(private))).all())
        proposals={}
        visible=[s.id for s in shipments if not s.hazardous and s.status not in ('completed','cancelled') and s.id not in private]
        if visible:
            pending=select(SlotProposal).where(SlotProposal.shipment_id.in_(visible),SlotProposal.status=='pending')
            if user['role']=='carrier':pending=pending.where(SlotProposal.carrier_id==user['org_id'])
            elif user['role']=='driver':pending=pending.where(SlotProposal.offer_id.in_([o.id for o in offers if o.status in ('waiting_driver','driver_ok','selected')]))
            for proposal in db.scalars(pending.order_by(SlotProposal.id)):
                if active_proposal(db,proposal):
                    actor=db.get(User,proposal.actor_id)
                    proposals.setdefault(proposal.shipment_id,[]).append(dict(id=proposal.id,day=proposal.day.isoformat(),
                        time=proposal.time,carrierId=proposal.carrier_id,role=actor.role,name=actor.name,offerId=proposal.offer_id))
        from board_orders import edit_scope, price_fields
        result=[]
        for s in shipments:
            item=shipment_dict(s,db)
            manual=private.get(s.id)
            if manual:
                item.update(internal=True,privateStage=manual.status,internalDriverId=manual.driver_id,internalVehicleId=manual.vehicle_id,
                    shipperName=manual.customer,carrier=db.get(Organization,manual.carrier_id).name,driver=db.get(Driver,manual.driver_id).name,
                    car=db.get(Vehicle,manual.vehicle_id).label)
            carrier_assigned=(s.selected_offer_id and db.get(Offer,s.selected_offer_id).carrier_id==user['org_id']) if user['role']=='carrier' else False
            if user['role']=='carrier' and not allowed(db,s,user['org_id']) and not carrier_assigned and not manual:
                item=sanitize_for_role(item,'carrier',external=True)
            elif user['role']=='driver':
                assigned=(manual and manual.status in ('selected','completed')) or any(o.shipment_id==s.id and o.status=='selected' for o in offers)
                known_parties=bool(manual) or any(o.shipment_id==s.id and allowed(db,s,o.carrier_id) for o in offers)
                item=sanitize_for_role(item,'driver',assigned=assigned,known_parties=known_parties)
            item['canEdit']=edit_scope(db,s,user)
            if item['canEdit']:
                item.update(price_fields(s,db))
            item['slotProposals']=proposals.get(s.id,[])
            if user['role']=='carrier':
                item['addressed']=db.get(DispatchRecipient,(s.id,user['org_id'])) is not None
            if s.hazardous:
                item['reviewOnly']=bool(dispatch_targets(db,s.id)) and not s.search_launched
            if user['role']=='shipper':
                item['recipients']=[{'carrierId':r.carrier_id,'name':db.get(Organization,r.carrier_id).name,
                    'status':'declined' if r.declined else 'review_seen' if r.review_seen else 'review_delivered' if s.hazardous else 'delivered'} for r in db.scalars(select(DispatchRecipient).where(DispatchRecipient.shipment_id==s.id))]
            result.append(item)
        notes=db.scalars(select(Notification).where(Notification.user_id==user['id']).order_by(Notification.id.desc()).limit(50)).all()
        current_org=db.get(Organization,user['org_id'])
        return {'user':{**{k:v for k,v in user.items() if k!='csrf'},
                        'organizationName':current_org.name if current_org else ''},'csrf':user['csrf'],
                'orders':result, 'offers':[({**offer_dict(o,db), 'price':0} if user['role']=='driver' else offer_dict(o,db)) for o in offers],
                'drivers':[{'id':d.id,'name':d.name,'userId':d.user_id} for d in drivers],
                'vehicles':[{'id':v.id,'label':v.label,'kind':v.kind,'capacity':float(v.capacity)} for v in vehicles],
                'notifications':[{'id':n.id,'kind':n.kind,'title':n.title,'message':n.message,'read':n.read,'orderId':db.get(Shipment,n.shipment_id).ref if n.shipment_id else None} for n in notes],
                'unreadNotifications':db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id==user['id'],Notification.read.is_(False))) or 0,
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

@app.get('/api/notifications')
def notification_history(before:int=0,user=Depends(user_session)):
    if before<0:invalid('Некорректный указатель истории',422)
    with SessionLocal() as db:
        query=select(Notification).where(Notification.user_id==user['id'])
        if before:query=query.where(Notification.id<before)
        notes=db.scalars(query.order_by(Notification.id.desc()).limit(51)).all()
        return {'hasMore':len(notes)>50,'notifications':[{'id':n.id,'kind':n.kind,'title':n.title,
                'message':n.message,'read':n.read,'orderId':db.get(Shipment,n.shipment_id).ref if n.shipment_id else None} for n in notes[:50]]}

@app.post('/api/notifications/read-all')
def read_all_notifications(request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:db.execute(update(Notification).where(Notification.user_id==user['id']).values(read=True))
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
    pickup_company: str = Field(default='',max_length=160)
    pickup_contact: str = Field(default='',max_length=200)
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
    return Shipment(creator_id=user['id'],creator_name=user.get('surname') or user['name'],first_price=payload.budget or None,ref=f'NEW-{secrets.token_hex(6).upper()}',shipper_id=user['org_id'],
            from_city=payload.from_city,to_city=payload.to_city,recipient=payload.recipient,
            product=payload.product,tons=payload.tons,pickup_date=payload.date,pickup_time=payload.time,
            unload_date=payload.unload or payload.date,vehicle_kind=payload.vehicle_type,mode=payload.mode,
            budget=payload.budget,rate=payload.tariff_per_ton,manager=payload.manager or user['name'],note=payload.note,
            hazardous=hazardous,status='search',epd='',etrn='',pickup_company=payload.pickup_company.strip(),
            pickup_contact=payload.pickup_contact.strip(),pickup_address=payload.pickup_address.strip(),
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
    if user['role']=='carrier':
        targets=dispatch_targets(db,s.id)
        if (allowed(db,s,user['org_id']) and (not targets or can_receive(db,s,user['org_id']))) or public_visible(db,s,user['org_id']):return s
    forbidden()

class SlotIn(BaseModel):
    offer_id: int | None = None
    day: date
    time: str = Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')
    expected_day: date
    expected_time: str = Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')

class SlotDecision(BaseModel):
    accepted: bool

def active_proposal(db, proposal):
    shipment=db.get(Shipment,proposal.shipment_id)
    if shipment.status not in ('search','choice','confirmed','enroute') or shipment.hazardous:
        return False
    if proposal.offer_id:
        offer=db.get(Offer,proposal.offer_id)
        return (offer.status=='selected' or (offer.status in ('waiting_driver','driver_ok') and (can_receive(db,shipment,offer.carrier_id) or public_visible(db,shipment,offer.carrier_id)))) and (
            not shipment.selected_offer_id or shipment.selected_offer_id==offer.id)
    if shipment.selected_offer_id:
        return db.get(Offer,shipment.selected_offer_id).carrier_id==proposal.carrier_id
    return shipment.search_launched and (can_receive(db,shipment,proposal.carrier_id) or public_visible(db,shipment,proposal.carrier_id))

def open_dispute(db,s,q):
    proposals=db.scalars(select(SlotProposal).where(SlotProposal.shipment_id==s.id,SlotProposal.carrier_id==q.carrier_id,
        SlotProposal.status=='pending',(SlotProposal.offer_id.is_(None)) | (SlotProposal.offer_id==q.id)))
    return any(active_proposal(db,p) for p in proposals)

def save_proposal(db,s,actor,carrier_id,offer,day,time):
    previous=db.scalars(select(SlotProposal).where(SlotProposal.shipment_id==s.id,
        SlotProposal.carrier_id==carrier_id,SlotProposal.actor_id==actor.id,SlotProposal.status=='pending')).all()
    for old in previous:
        if old.day==day and old.time==time and active_proposal(db,old):
            if offer and old.offer_id is None:old.offer_id=offer.id
            return old,False
        old.status='superseded'
    item=SlotProposal(shipment_id=s.id,offer_id=offer.id if offer else None,carrier_id=carrier_id,
                      actor_id=actor.id,day=day,time=time,status='pending')
    db.add(item);db.flush()
    return item,True

def update_slot(db,s,day,time,related=None):
    chosen=db.get(Offer,s.selected_offer_id) if s.selected_offer_id else None
    resource=chosen or related
    deadline=(resource.delivery_day or max(s.unload_date,resource.pickup_date)) if resource else s.unload_date
    if day<business_today() or day>deadline:
        invalid('Дата подачи должна быть между сегодняшней датой и выгрузкой',422)
    old=official_slot(s,chosen)
    if resource:
        if day>resource.end_date or conflicts(db,resource.driver_id,resource.vehicle_id,day,resource.end_date,
                exclude_shipment=s.id,include_driver_accepted=True):
            invalid('В этот период водитель в отпуске или экипаж занят другим рейсом')
        resource.pickup_date,resource.pickup_time=day,time
    # The shipper closes the pending time proposals in this transaction. A crew
    # offered solely at a different time must be free to submit a fresh bid.
    for bid in db.scalars(select(Offer).where(Offer.shipment_id==s.id,Offer.status.in_(['waiting_driver','driver_ok']))):
        if bid is resource:
            continue
        if (bid.pickup_date,bid.pickup_time) not in (old,(day,time)):
            bid.status='rejected'
            continue
        last=bid.delivery_day or max(s.unload_date,bid.pickup_date)
        if day>min(last,bid.end_date) or conflicts(db,bid.driver_id,bid.vehicle_id,day,bid.end_date,
                exclude_shipment=s.id,include_driver_accepted=True):
            bid.status='withdrawn'
        else:
            bid.pickup_date,bid.pickup_time=day,time
    s.slot_day,s.slot_time=day,time
    db.flush()
    if s.status=='choice' and not db.scalar(select(Offer.id).where(Offer.shipment_id==s.id,Offer.status=='driver_ok').limit(1)):
        s.status='search'

@app.post('/api/orders/{ref}/slot')
def propose_or_set_slot(ref:str,payload:SlotIn,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        s=db.scalar(select(Shipment).where(Shipment.ref==ref))
        if not s or db.get(InternalTrip,s.id):raise HTTPException(404,detail='Заявка не найдена')
        role=user['role'];actor=db.get(User,user['id'])
        selected=db.get(Offer,s.selected_offer_id) if s.selected_offer_id else None
        q=None
        if role=='shipper':
            if s.shipper_id!=user['org_id']:forbidden()
        else:
            related=select(Offer).where(Offer.shipment_id==s.id,Offer.status.in_(['waiting_driver','driver_ok','selected']))
            if role=='driver':
                mine=related.join(Driver,Driver.id==Offer.driver_id).where(Driver.user_id==actor.id)
                if payload.offer_id:mine=mine.where(Offer.id==payload.offer_id)
                own=db.scalars(mine.limit(2)).all()
                if not own:forbidden()
                if len(own)>1:invalid('Выберите перевозчика, по рейсу которого меняется время',422)
                q=own[0]
            elif role=='carrier':
                q=db.scalar(related.where(Offer.carrier_id==actor.org_id))
                if selected and selected.carrier_id!=actor.org_id:forbidden()
                if not q and not (s.search_launched and (can_receive(db,s,actor.org_id) or public_visible(db,s,actor.org_id))):forbidden()
            else:forbidden()
        if s.status not in ('search','choice','confirmed') or s.hazardous:invalid('На этой стадии изменение подачи недоступно')
        current=official_slot(s,selected)
        if current!=(payload.expected_day,payload.expected_time):
            invalid('Заказчик уже изменил время. Обновите карточку и проверьте очередь')
        if (payload.day,payload.time)==current:
            if role=='shipper':return {'ok':True,'changed':False}
            cid=q.carrier_id if q else actor.org_id
            mine=select(SlotProposal).where(SlotProposal.shipment_id==s.id,SlotProposal.carrier_id==cid,
                                             SlotProposal.actor_id==actor.id,SlotProposal.status=='pending')
            own=db.scalars(mine).all()
            for old in own:old.status='withdrawn'
            adjust=bool(q and q.status in ('waiting_driver','driver_ok') and (q.pickup_date,q.pickup_time)!=current
                        and not open_dispute(db,s,q))
            if adjust:
                last=q.delivery_day or max(s.unload_date,q.pickup_date)
                if not business_today()<=current[0]<=min(last,q.end_date) or conflicts(db,q.driver_id,q.vehicle_id,current[0],q.end_date,
                        exclude_shipment=s.id,include_driver_accepted=True):invalid('Экипаж недоступен в даты заказчика')
                q.pickup_date,q.pickup_time=current
            if own or adjust:audit(db,actor,s,'slot_withdrawn',f'{current[0]} {current[1]}')
            return {'ok':True,'changed':bool(own or adjust)}

        if role=='shipper':
            update_slot(db,s,payload.day,payload.time)
            db.execute(update(SlotProposal).where(SlotProposal.shipment_id==s.id,SlotProposal.status=='pending').values(status='superseded'))
            audit(db,actor,s,'slot_changed',f'{payload.day} {payload.time}')
            return {'ok':True,'changed':True}
        deadline=(q.delivery_day or max(s.unload_date,q.pickup_date)) if q else s.unload_date
        if not business_today()<=payload.day<=deadline:invalid('Дата подачи выходит за период рейса',422)
        proposal,changed=save_proposal(db,s,actor,q.carrier_id if q else actor.org_id,q,payload.day,payload.time)
        if changed:audit(db,actor,s,'slot_proposed',f'{payload.day} {payload.time}')
        return {'ok':True,'id':proposal.id,'changed':changed}

@app.post('/api/orders/{ref}/slot/{proposal_id}')
def decide_slot(ref:str,proposal_id:int,payload:SlotDecision,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper':forbidden()
    with db_write() as db:
        s=get_visible_shipment(db,ref,user)
        item=db.get(SlotProposal,proposal_id)
        if not item or item.shipment_id!=s.id:raise HTTPException(404,detail='Предложение времени не найдено')
        target='approved' if payload.accepted else 'rejected'
        if item.status==target:return {'ok':True}
        if item.status!='pending' or not active_proposal(db,item):invalid('Предложение уже закрыто. Обновите очередь')
        q=db.get(Offer,item.offer_id) if item.offer_id else db.scalar(select(Offer).where(Offer.shipment_id==s.id,
            Offer.carrier_id==item.carrier_id,Offer.status.in_(['waiting_driver','driver_ok','selected'])))
        if payload.accepted:
            if item.new_unload is not None:
                from board_orders import update_unload
                update_unload(db,s,item.new_unload,item.day)
            update_slot(db,s,item.day,item.time,q)
            db.execute(update(SlotProposal).where(SlotProposal.shipment_id==s.id,SlotProposal.status=='pending',
                SlotProposal.id!=item.id).values(status='superseded'))
        elif q and q.status in ('waiting_driver','driver_ok') and (q.pickup_date,q.pickup_time)!=(official_slot(s)):
            # Neither party has committed resources at the rejected time.
            q.status='rejected'
            if s.status=='choice' and not db.scalar(select(Offer.id).where(Offer.shipment_id==s.id,Offer.status=='driver_ok')):
                s.status='search'
        item.status=target
        audit(db,db.get(User,user['id']),s,'slot_accepted' if payload.accepted else 'slot_rejected',
              f'{item.day} {item.time} · {item.id}')
        return {'ok':True}

class LeaveIn(BaseModel):
    start: date
    end: date

def leave_dict(row):
    return {'id':row.id,'start':row.start.isoformat(),'end':row.end.isoformat()}

def leave_owner(db,user):
    if user['role']!='driver':forbidden()
    driver=db.scalar(select(Driver).where(Driver.user_id==user['id']))
    if not driver:invalid('Карточка водителя отсутствует')
    return driver

@app.post('/api/driver/leave',status_code=201)
def add_leave(payload:LeaveIn,request:Request,response:Response,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        driver=leave_owner(db,user)
        same=db.scalar(select(DriverLeave).where(DriverLeave.driver_id==driver.id,DriverLeave.start==payload.start,DriverLeave.end==payload.end))
        if same:
            response.status_code=200
            return leave_dict(same)
        if payload.start<business_today() or payload.end<payload.start:invalid('Проверьте даты начала и окончания отпуска',422)
        if conflicts(db,driver.id,-1,payload.start,payload.end,include_driver_accepted=True):
            invalid('Есть пересечение с отпуском, резервом или подтверждённым рейсом')
        leave=DriverLeave(driver_id=driver.id,start=payload.start,end=payload.end)
        db.add(leave);db.flush()
        return leave_dict(leave)

@app.post('/api/driver/leave/{leave_id}/remove')
def remove_leave(leave_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        driver=leave_owner(db,user)
        db.execute(delete(DriverLeave).where(DriverLeave.id==leave_id,DriverLeave.driver_id==driver.id))
    return {'ok':True}

class InnIn(BaseModel):
    inn: str = Field(max_length=12)

@app.post('/api/account/inn')
def set_company_inn(payload:InnIn,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role'] not in ('shipper','carrier') or not user['is_admin']:forbidden()
    with db_write() as db:
        org=db.get(Organization,user['org_id'])
        inn=company_inn(payload.inn)
        if db.get(InnRestriction,inn):forbidden('Доступ по этому ИНН закрыт')
        org.inn=inn
        return {'inn':org.inn}


class CreateOffer(BaseModel):
    order_ref:str
    driver_id:int
    vehicle_id:int
    day:date
    pickup:str=Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    end_date:date
    delivery_day:date | None = None
    price:int=Field(ge=1000,le=50_000_000)
    accept_terms: bool = False


def intersects(a1:date,a2:date,b1:date,b2:date):
    return a1<=b2 and b1<=a2

def conflicts(db:Session, driver_id:int, vehicle_id:int, start:date, end:date,
              exclude_shipment:int|None=None, *, include_driver_accepted:bool=False):
    if db.scalar(select(DriverLeave.id).where(DriverLeave.driver_id==driver_id,DriverLeave.start<=end,DriverLeave.end>=start).limit(1)):
        return True
    manual=select(InternalTrip.shipment_id).join(Shipment,Shipment.id==InternalTrip.shipment_id).where(
        InternalTrip.status=='selected',(InternalTrip.driver_id==driver_id)|(InternalTrip.vehicle_id==vehicle_id),
        Shipment.pickup_date<=end,Shipment.unload_date>=start)
    stages=['selected','driver_ok'] if include_driver_accepted else ['selected']
    booked=select(Offer.id).join(Shipment,Shipment.id==Offer.shipment_id).where(
        Offer.status.in_(stages),Shipment.status.notin_(['cancelled','completed']),
        (Offer.driver_id==driver_id)|(Offer.vehicle_id==vehicle_id),Offer.pickup_date<=end,Offer.end_date>=start)
    if exclude_shipment:
        manual=manual.where(InternalTrip.shipment_id!=exclude_shipment)
        booked=booked.where(Offer.shipment_id!=exclude_shipment)
    return bool(db.scalar(manual.limit(1)) or db.scalar(booked.limit(1)))

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
        if not d or not v or not carrier_has_driver(db,user['org_id'],d) or v.org_id!=user['org_id']:forbidden('Чужой водитель или автомобиль')
        driver_account=db.get(User,d.user_id) if d.user_id else None
        if driver_account and not driver_account.active:invalid('Учётная запись водителя недоступна')
        if v.kind!=s.vehicle_kind or v.capacity<s.tons:invalid('Тип или грузоподъёмность транспорта не соответствуют заявке')
        if payload.end_date<payload.day:invalid('Конец рейса раньше начала',422)
        if payload.day < business_today():invalid('Дата подачи не может быть в прошлом',422)
        if payload.end_date<s.unload_date:invalid('Окончание занятости не может быть раньше плановой выгрузки',422)
        if conflicts(db,d.id,v.id,payload.day,payload.end_date,include_driver_accepted=True):invalid('Автомобиль или водитель заняты либо зарезервированы в указанный период')
        delivery_day=payload.delivery_day or max(s.unload_date,payload.day)
        if not payload.day<=delivery_day<=payload.end_date:
            invalid('Дата выгрузки должна попадать в период рейса',422)
        q=Offer(shipment_id=s.id,carrier_id=user['org_id'],driver_id=d.id,vehicle_id=v.id,price=payload.price,delivery_day=delivery_day,
                pickup_date=payload.day,pickup_time=payload.pickup,end_date=payload.end_date,status='waiting_driver',
                terms_accepted=True,accepted_terms_version=s.terms_version)
        db.add(q);db.flush()
        if (q.pickup_date,q.pickup_time)!=official_slot(s):
            save_proposal(db,s,db.get(User,user['id']),q.carrier_id,q,q.pickup_date,q.pickup_time)
        audit(db,db.get(User,user['id']),s,'offer_created',f'offer={q.id}')
        if d.user_id:note_user(db,d.user_id,s.id,'crew_reserved','Рейс в вашем графике',f'{s.ref} · Подал перевозчик. Подтверждение не требуется.')
        q.status='driver_ok';s.status='choice'
        db.flush()
        if allowed(db,s,q.carrier_id) and q.price==s.budget and s.budget>0 and (q.pickup_date,q.pickup_time)==official_slot(s) and not open_dispute(db,s,q):
            assign_offer(db,q,s,db.get(User,user['id']),'auto_assigned')
        result=offer_dict(q,db)
    return result

def assign_offer(db:Session, q:Offer, s:Shipment, actor:User, action:str):
    if s.status not in ('search','choice') or s.selected_offer_id is not None: invalid('Заявка уже назначена')
    if q.status!='driver_ok':invalid('Перевозчик не зарезервировал экипаж')
    if (q.pickup_date,q.pickup_time)!=official_slot(s) or open_dispute(db,s,q):
        invalid('Сначала согласуйте встречное время в очереди заказчика')
    if s.hazardous:invalid('Для опасного груза требуется отдельная проверка допусков')
    if not allowed(db,s,q.carrier_id): invalid('Новый перевозчик ещё не принят в пул заказчика')
    if not q.terms_accepted or q.accepted_terms_version!=s.terms_version:invalid('Требования заказчика не приняты')
    v=db.get(Vehicle,q.vehicle_id)
    if not v or v.kind!=s.vehicle_kind or v.capacity<s.tons: invalid('Автомобиль не соответствует заявке')
    driver = db.get(Driver, q.driver_id)
    account = db.get(User, driver.user_id) if driver and driver.user_id else None
    if not driver or not carrier_has_driver(db,q.carrier_id,driver) or (account and not account.active):invalid('Экипаж перевозчика недоступен')
    if q.pickup_date < business_today():invalid('Дата подачи уже прошла. Требуется новое предложение')
    if conflicts(db,q.driver_id,q.vehicle_id,q.pickup_date,q.end_date,exclude_shipment=s.id,include_driver_accepted=True):invalid('Машина или водитель уже заняты')
    if s.first_price is None:s.first_price=s.budget or q.price
    if q.price!=s.first_price:s.price_revised=True
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
        if accepted and q.status in ('driver_ok','selected'):return {'ok':True,'required':False}
        if q.status!='waiting_driver':invalid('Предложение уже обработано')
        if s.status not in ('search','choice'):invalid('Заявка закрыта')
        if s.hazardous:invalid('Для опасного груза назначение заблокировано')
        if accepted and q.pickup_date < business_today():invalid('Дата подачи уже прошла')
        if accepted and conflicts(db,q.driver_id,q.vehicle_id,q.pickup_date,q.end_date,
                                  exclude_shipment=s.id,include_driver_accepted=True):
            invalid('Водитель или автомобиль уже заняты либо подтвердили пересекающееся предложение')
        q.status='driver_ok' if accepted else 'declined'
        if accepted:
            s.status='choice'
            member=db.scalar(select(RosterMember).where(RosterMember.driver_id==driver.id,RosterMember.carrier_id==q.carrier_id,RosterMember.active.is_(True)))
            if member:member.share_busy=True
        audit(db,db.get(User,user['id']),s,'driver_ok' if accepted else 'driver_declined',f'offer={q.id}')
        note_org(db,q.carrier_id,'carrier',s.id,'driver_ok' if accepted else 'driver_declined',
                 'Ответ водителя',f'{s.ref}: водитель {"подтвердил" if accepted else "отклонил"} рейс')
        if (accepted and allowed(db,s,q.carrier_id) and q.price==s.budget and s.budget>0
                and (q.pickup_date,q.pickup_time)==official_slot(s) and not open_dispute(db,s,q)):
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
    from board_orders import cancel
    return cancel(ref,request,user)

@app.post('/api/orders/{ref}/decline-incoming')
def decline_incoming(ref:str,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier':forbidden()
    with db_write() as db:
        shipment=db.scalar(select(Shipment).where(Shipment.ref==ref))
        recipient=db.get(DispatchRecipient,(shipment.id,user['org_id'])) if shipment else None
        if not recipient or not allowed(db,shipment,user['org_id']): raise HTTPException(404,detail='Входящая заявка не найдена')
        if recipient.declined:return {'ok':True}
        if shipment.status not in ('search','choice'):invalid('Заявка уже закрыта')
        if db.scalar(select(Offer.id).where(Offer.shipment_id==shipment.id,Offer.carrier_id==user['org_id'],Offer.status.in_(['waiting_driver','driver_ok','selected']))):
            invalid('Сначала отзовите действующее предложение')
        recipient.declined=True
        audit(db,db.get(User,user['id']),shipment,'carrier_declined','Получатель отклонил запрос')
    return {'ok':True}

class InternalTripIn(BaseModel):
    request_id: str = Field(min_length=12,max_length=72)
    customer: str = Field(min_length=2,max_length=160)
    driver_id: int
    vehicle_id: int
    shipment: CreateShipment

@app.post('/api/carrier/plan/trips',status_code=201)
def plan_internal_trip(payload:InternalTripIn,request:Request,response:Response,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier':forbidden()
    signature=sha_token(payload.model_dump_json())
    with db_write() as db:
        repeat=db.scalar(select(InternalTrip).where(InternalTrip.carrier_id==user['org_id'],InternalTrip.request_id==payload.request_id))
        if repeat:
            if repeat.signature!=signature:invalid('Этот запрос уже использован для другой записи')
            response.status_code=200
            return {'id':db.get(Shipment,repeat.shipment_id).ref,'replayed':True}
        driver=db.get(Driver,payload.driver_id);vehicle=db.get(Vehicle,payload.vehicle_id)
        if not driver or not carrier_has_driver(db,user['org_id'],driver) or not vehicle or vehicle.org_id!=user['org_id']:forbidden()
        account=db.get(User,driver.user_id) if driver.user_id else None
        if account and not account.active:invalid('Кабинет водителя отключён')
        shipment=build_shipment(payload.shipment,user)
        if shipment.hazardous:invalid('Для этой категории груза нужен отдельный процесс проверки допуска')
        if not shipment.pickup_company:invalid('Укажите грузоотправителя',422)
        if shipment.vehicle_kind!=vehicle.kind or shipment.tons>vehicle.capacity:invalid('Машина не подходит по типу или грузоподъёмности')
        if conflicts(db,driver.id,vehicle.id,shipment.pickup_date,shipment.unload_date,include_driver_accepted=True):invalid('Машина или водитель заняты в эти даты')
        # The carrier owns this private record; a customer’s name cannot grant another organization access.
        shipment.public_external=False;shipment.search_launched=False
        db.add(shipment);db.flush()
        db.add(InternalTrip(shipment_id=shipment.id,carrier_id=user['org_id'],customer=clean_label(payload.customer),
            driver_id=driver.id,vehicle_id=vehicle.id,request_id=payload.request_id,signature=signature,status='selected'))
        shipment.status='confirmed'
        db.flush()
        audit(db,db.get(User,user['id']),shipment,'internal_created','Внутренний рейс диспетчера')
        return {'id':shipment.ref,'replayed':False}

@app.post('/api/carrier/plan/trips/{ref}/{action}')
def act_on_internal_trip(ref:str,action:Literal['accept','decline','cancel','complete'],request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if action=='cancel':
        from board_orders import cancel
        return cancel(ref,request,user)
    with db_write() as db:
        shipment=db.scalar(select(Shipment).where(Shipment.ref==ref))
        trip=db.get(InternalTrip,shipment.id) if shipment else None
        if not trip:raise HTTPException(404,detail='Рейс не найден')
        driver=db.get(Driver,trip.driver_id)
        if action in ('accept','decline'):
            if user['role']!='driver' or driver.user_id!=user['id']:forbidden()
        elif user['role']!='carrier' or trip.carrier_id!=user['org_id']:forbidden()
        target={'accept':'selected','decline':'declined','cancel':'cancelled','complete':'completed'}[action]
        if trip.status==target:return {'ok':True}
        if action in ('accept','decline') and trip.status!='waiting_driver':invalid('Рейс уже обработан')
        if action=='cancel' and trip.status not in ('waiting_driver','selected'):invalid('Рейс уже закрыт')
        if action=='complete' and trip.status!='selected':invalid('Рейс ещё не назначен')
        if action=='accept':
            if shipment.pickup_date<business_today():invalid('Дата подачи уже прошла')
            if conflicts(db,driver.id,trip.vehicle_id,shipment.pickup_date,shipment.unload_date,include_driver_accepted=True):
                invalid('На эти даты уже есть подтверждённый рейс или резерв')
            link=db.scalar(select(RosterMember).where(RosterMember.driver_id==driver.id,RosterMember.carrier_id==trip.carrier_id,RosterMember.active.is_(True)))
            if link:link.share_busy=True
        trip.status=target
        shipment.status={'selected':'confirmed','declined':'cancelled','cancelled':'cancelled','completed':'completed'}[target]
        audit(db,db.get(User,user['id']),shipment,'internal_'+action,'Изменение личного графика')
    return {'ok':True}

def internal_busy(db:Session,driver_id:int,carrier_id:int|None=None):
    query=select(InternalTrip.shipment_id).where(InternalTrip.driver_id==driver_id,InternalTrip.status.in_(['waiting_driver','selected']))
    if carrier_id is not None:query=query.where(InternalTrip.carrier_id==carrier_id)
    return db.scalar(query.limit(1)) is not None

class ReviewRecipients(BaseModel):
    carrier_ids: list[int] = Field(min_length=1)

@app.post('/api/orders/{ref}/request-review')
def request_cargo_review(ref:str,payload:ReviewRecipients,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper':forbidden()
    with db_write() as db:
        s=get_visible_shipment(db,ref,user)
        if not s.hazardous or s.search_launched or s.status!='search':invalid('Запрос проверки недоступен для этой заявки')
        targets=set(payload.carrier_ids)
        for cid in targets:
            if not allowed(db,s,cid) or not db.scalar(select(User.id).where(User.org_id==cid,User.role=='carrier',User.active.is_(True)).limit(1)):
                invalid('Для проверки выберите подключённого перевозчика с активным кабинетом',422)
        existing=dispatch_targets(db,s.id)
        if existing:
            if targets!=existing:invalid('Запрос уже доставлен выбранным получателям')
            return {'ok':True,'replayed':True,'companies':len(existing)}
        for cid in targets:db.add(DispatchRecipient(shipment_id=s.id,carrier_id=cid))
        db.flush()
        audit(db,db.get(User,user['id']),s,'cargo_review_requested','Направлена заявка для предварительной проверки')
        return {'ok':True,'replayed':False,'companies':len(targets)}

@app.post('/api/orders/{ref}/acknowledge-review')
def acknowledge_review(ref:str,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier':forbidden()
    with db_write() as db:
        s=db.scalar(select(Shipment).where(Shipment.ref==ref))
        r=db.get(DispatchRecipient,(s.id,user['org_id'])) if s else None
        if not r or not allowed(db,s,user['org_id']):raise HTTPException(404,detail='Запрос не найден')
        if not s.hazardous or s.status!='search' or r.declined:invalid('Запрос проверки уже закрыт')
        if not r.review_seen:
            r.review_seen=True
            audit(db,db.get(User,user['id']),s,'cargo_review_received','Перевозчик принял запрос на проверку')
        return {'ok':True,'movementAuthorized':False}

class PublishIn(BaseModel):
    public_external: bool = False
    carrier_ids: list[int] | None = None

@app.post('/api/orders/{ref}/search')
def publish_search(ref:str,payload:PublishIn,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper':forbidden()
    with db_write() as db:
        s=get_visible_shipment(db,ref,user)
        if s.status!='search' or s.search_launched:invalid('Поиск уже запущен или заявка назначена')
        if s.hazardous:invalid('Публикация поиска опасного или неподтверждённого груза в пилоте заблокирована')
        if official_slot(s)[0] < business_today():invalid('Дата погрузки уже прошла')
        target_ids=set(payload.carrier_ids or [])
        if payload.carrier_ids is not None:
            if not target_ids or payload.public_external:
                invalid('Выберите получателей адресной заявки',422)
            for cid in target_ids:
                if not allowed(db,s,cid):invalid('Выберите действующего партнёра',422)
                if not db.scalar(select(User.id).where(User.org_id==cid,User.role=='carrier',User.active.is_(True)).limit(1)):
                    invalid('У выбранного перевозчика нет активного диспетчера')
                db.add(DispatchRecipient(shipment_id=s.id,carrier_id=cid))
            db.flush()
        s.search_launched=True
        s.public_external=payload.public_external and not s.hazardous
        carriers={a.carrier_id for a in db.scalars(select(Approval).where(Approval.shipper_id==s.shipper_id,Approval.active.is_(True)))}
        if s.public_external:
            carriers.update(o.id for o in db.scalars(select(Organization).where(Organization.kind=='carrier')) if fleet_suitable(db,s,o.id))
        notified=0
        for carrier_id in target_ids or carriers:
            if not target_ids and not fleet_suitable(db,s,carrier_id):continue
            for u in db.scalars(select(User).where(User.org_id==carrier_id,User.role=='carrier',User.active.is_(True))):
                note_user(db,u.id,s.id,'new_order','Новая заявка',f'Новый рейс: {s.from_city} → {s.to_city}, {float(s.tons):g} т')
                notified+=1
        audit(db,db.get(User,user['id']),s,'search_launched',f'notified={notified};public={s.public_external}')
    return {'ok':True,'notified':notified,'publicExternal':s.public_external}

@app.post('/api/shipper/carriers/{carrier_id}/approve')
def approve_carrier(carrier_id:int,request:Request,user=Depends(user_session)):
    from board_partners import invite_company, CompanyTarget
    return invite_company(CompanyTarget(company_id=carrier_id),request,Response(),user)

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

def carrier_has_driver(db:Session, carrier_id:int, driver:Driver) -> bool:
    return bool(driver.user_id and driver.org_id==carrier_id) or db.scalar(select(RosterMember.id).where(
        RosterMember.carrier_id==carrier_id,RosterMember.driver_id==driver.id,RosterMember.active.is_(True))) is not None

def carrier_drivers(db:Session, carrier_id:int):
    roster_ids=select(RosterMember.driver_id).where(RosterMember.carrier_id==carrier_id,RosterMember.active.is_(True))
    return db.scalars(select(Driver).outerjoin(User,User.id==Driver.user_id).where(
        (((Driver.org_id==carrier_id) & Driver.user_id.is_not(None)) | Driver.id.in_(roster_ids)),
        ((Driver.user_id.is_(None)) | User.active.is_(True))).order_by(Driver.name,Driver.id)).all()

class RosterIn(BaseModel):
    name: str = Field(min_length=2,max_length=100)
    email: str = Field(default='',max_length=180)

@app.post('/api/carrier/roster',status_code=201)
def add_roster_member(payload:RosterIn,request:Request,response:Response,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier':forbidden()
    name=clean_label(payload.name)
    email=normalize_email(payload.email) if payload.email.strip() else None
    cid=user['org_id']
    with db_write() as db:
        condition=RosterMember.email==email if email else (RosterMember.email.is_(None) & (RosterMember.name==name))
        member=db.scalar(select(RosterMember).where(RosterMember.carrier_id==cid,condition))
        if member:
            if not member.active:member.share_busy=False
            member.active=True
            response.status_code=200
            driver=db.get(Driver,member.driver_id)
        else:
            account=db.scalar(select(User).where(User.email==email)) if email else None
            if account and (account.role!='driver' or not account.active):
                invalid('Этот адрес нельзя использовать для назначения водителя',422)
            driver=db.scalar(select(Driver).where(Driver.user_id==account.id)) if account else None
            if not driver:
                # A private roster entry is usable for planning before its owner has a login.
                driver=Driver(org_id=cid,user_id=None,name=name)
                db.add(driver);db.flush()
            db.add(RosterMember(carrier_id=cid,driver_id=driver.id,name=name,email=email))
        return {'id':driver.id,'name':name,'canReceive':bool(driver.user_id)}

class RosterContactIn(BaseModel):
    email:str=Field(min_length=6,max_length=180)

@app.post('/api/carrier/roster/{driver_id}/contact')
def connect_roster_contact(driver_id:int,payload:RosterContactIn,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier':forbidden()
    email=normalize_email(payload.email)
    with db_write() as db:
        member=db.scalar(select(RosterMember).where(RosterMember.carrier_id==user['org_id'],RosterMember.driver_id==driver_id,RosterMember.active.is_(True)))
        if not member:raise HTTPException(404,detail='Карточка не найдена')
        person=db.get(Driver,member.driver_id)
        if person.user_id:
            if member.email!=email:invalid('Карточка уже связана с личным кабинетом')
            return {'id':person.id,'canReceive':True}
        duplicate=db.scalar(select(RosterMember.id).where(RosterMember.carrier_id==member.carrier_id,RosterMember.email==email,RosterMember.id!=member.id))
        if duplicate:invalid('Этот адрес уже есть в пуле')
        account=db.scalar(select(User).where(User.email==email))
        if account and (account.role!='driver' or not account.active):invalid('Укажите почту активного водителя',422)
        member.email=email
        if account:
            canonical=db.scalar(select(Driver).where(Driver.user_id==account.id))
            if not canonical:invalid('Личный кабинет водителя недоступен')
            bind_roster_driver(db,member,canonical)
        return {'id':member.driver_id,'canReceive':bool(account)}

class AddDriver(BaseModel):
    name: str = Field(min_length=2,max_length=100)
    email: str = Field(min_length=6,max_length=180)
    password: str = Field(min_length=12,max_length=128)

@app.post('/api/carrier/drivers',status_code=201)
def add_driver(payload:AddDriver,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier':forbidden()
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
        ships=db.scalars(select(Shipment).outerjoin(Offer,Offer.id==Shipment.selected_offer_id)
            .where(Shipment.shipper_id==user['org_id']).order_by(
                func.coalesce(Shipment.slot_day,Offer.pickup_date,Shipment.pickup_date),
                func.coalesce(Shipment.slot_time,Offer.pickup_time,Shipment.pickup_time),Shipment.id)).all()
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
    surname: str = Field(default='',max_length=100)
    inn: str = Field(default='',max_length=12)
    role: Literal['shipper','carrier','driver']
    name: str = Field(min_length=2,max_length=100)
    email: str = Field(min_length=6,max_length=180)
    password: str = Field(min_length=12,max_length=128)
    organization: str = Field(default='',max_length=160)
    invitation_token: str | None = Field(default=None,min_length=32,max_length=200)
    vehicle_kind: Literal['Автоцистерна','Фура'] | None = None
    vehicle_label: str = Field(default='',max_length=100)
    capacity_tons: Decimal | None = Field(default=None,gt=0,le=100,max_digits=7,decimal_places=2)

def company_inn(value: str) -> str:
    value=value.strip()
    if not re.fullmatch(r'[0-9]{10}|[0-9]{12}',value) or len(set(value))==1:
        invalid('Укажите ИНН организации из 10 цифр или ИНН ИП из 12 цифр',422)
    digits=list(map(int,value))
    def check(weights):
        return sum(weight*digit for weight,digit in zip(weights,digits)) % 11 % 10
    checks=([2,4,10,3,5,9,4,6,8],) if len(value)==10 else ([7,2,4,10,3,5,9,4,6,8],[3,7,2,4,10,3,5,9,4,6,8])
    if any(check(weights)!=digits[len(weights)] for weights in checks):
        invalid('Проверьте цифры ИНН',422)
    return value

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

def bind_roster_driver(db:Session,member:RosterMember,driver:Driver):
    placeholder=db.get(Driver,member.driver_id)
    if placeholder.user_id is not None or placeholder.org_id!=member.carrier_id:invalid('Карточка уже привязана')
    for q in db.scalars(select(Offer).where(Offer.driver_id==placeholder.id)):
        q.driver_id=driver.id
        if q.status in ('waiting_driver','driver_ok','selected'):
            note_user(db,driver.user_id,q.shipment_id,'crew_reserved','График обновлён','Рейс доступен в вашем календаре')
    for trip in db.scalars(select(InternalTrip).where(InternalTrip.driver_id==placeholder.id)):
        trip.driver_id=driver.id
        if trip.status in ('waiting_driver','selected'):note_user(db,driver.user_id,trip.shipment_id,'crew_reserved','График обновлён','Рейс доступен в вашем календаре')
    member.driver_id=driver.id
    db.flush()
    db.delete(placeholder)

def attach_team(db: Session, target: User, org_id: int):
    if target.org_id==org_id: return
    old_org=db.get(Organization,target.org_id)
    if target.role!='driver' or old_org.kind!='driver': invalid('Этот участник уже состоит в другой компании')
    driver=db.scalar(select(Driver).where(Driver.user_id==target.id))
    if not driver: invalid('Профиль водителя не найден')
    if driver_busy(db,driver.id):invalid('Сначала завершите действующие предложения и рейсы водителя')
    pending=db.scalar(select(RosterMember).where(RosterMember.carrier_id==org_id,RosterMember.email==target.email))
    if pending and pending.driver_id!=driver.id:bind_roster_driver(db,pending,driver)
    driver.org_id=org_id;target.org_id=org_id;target.is_admin=False
    # A second device must refresh the changed membership before continuing.
    db.execute(delete(WebSession).where(WebSession.user_id==target.id))

def accept_invitation(db: Session, inv: ParticipantInvitation, recipient: User):
    check_invitation(inv)
    sender=db.get(User,inv.sender_user_id)
    if not sender or not sender.active or db.get(Organization,sender.org_id).disposition!='active' or db.get(Organization,recipient.org_id).disposition!='active' or (sender.org_id!=inv.sender_org_id and inv.kind!='driver_join'):
        invalid('Автор приглашения больше не имеет доступа к компании')
    if recipient.role!=inv.target_role or recipient.email!=inv.target_email: forbidden('Приглашение предназначено другому участнику')
    if inv.kind=='peer':
        pair=tuple(sorted((sender.org_id,recipient.org_id)))
        if not db.get(CompanyPeer,pair):db.add(CompanyPeer(first_id=pair[0],second_id=pair[1]))
    elif inv.kind=='driver_shipper':
        row=db.scalar(select(ShipperDriver).where(ShipperDriver.shipper_id==recipient.org_id,ShipperDriver.user_id==sender.id))
        if row:row.active=True
        else:db.add(ShipperDriver(shipper_id=recipient.org_id,user_id=sender.id,active=True))
    elif inv.kind=='partner':
        if {sender.role,recipient.role}!={'shipper','carrier'}: invalid('Несовместимые роли партнёров')
        activate_partner(db,sender.org_id if sender.role=='shipper' else recipient.org_id,
                         sender.org_id if sender.role=='carrier' else recipient.org_id)
    elif inv.kind=='team':
        if not sender.is_admin: forbidden('Автор больше не управляет сотрудниками')
        attach_team(db,recipient,sender.org_id)
    elif inv.kind=='driver_join':
        if recipient.role!='carrier' or not recipient.is_admin: forbidden('Подключить водителя может руководитель перевозчика')
        if db.get(Organization,sender.org_id).kind=='driver':
            attach_team(db,sender,recipient.org_id)
        elif sender.org_id!=recipient.org_id:
            driver=db.scalar(select(Driver).where(Driver.user_id==sender.id))
            member=db.scalar(select(RosterMember).where(RosterMember.carrier_id==recipient.org_id,RosterMember.email==sender.email))
            if member:
                if member.driver_id!=driver.id:
                    bind_roster_driver(db,member,driver)
                member.active=True;member.share_busy=True
            else:db.add(RosterMember(carrier_id=recipient.org_id,driver_id=driver.id,email=sender.email,name=sender.name,share_busy=True))
    elif inv.kind=='shipper_driver':
        if sender.role!='shipper' or recipient.role!='driver':forbidden()
        link=db.scalar(select(ShipperDriver).where(ShipperDriver.shipper_id==sender.org_id,ShipperDriver.user_id==recipient.id))
        if link:link.active=True
        else:db.add(ShipperDriver(shipper_id=sender.org_id,user_id=recipient.id,active=True))
    else: invalid('Неизвестный вид приглашения')
    inv.status='accepted';inv.target_user_id=recipient.id;inv.responded_at=datetime.now(timezone.utc)
    note_user(db,sender.id,None,'invitation_accepted','Приглашение принято','Участник подтвердил подключение')
    note_user(db,recipient.id,None,'invitation_accepted','Подключение подтверждено','Список участников обновлён')

@app.get('/api/config')
def public_config():
    from push_delivery import public_key
    return {'version':VERSION,'signupEnabled':os.getenv('LOGISTIK_ALLOW_SIGNUP','0')=='1',
            'roles':['shipper','carrier','driver'],'emailDelivery':False,
            'recoveryMethod':'one_time_code','pushPublicKey':public_key(),
            'pushEnabled':bool(public_key())}

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
            org=Organization(name=organization,kind=payload.role,inn=company_inn(payload.inn) if payload.role!='driver' else None);db.add(org);db.flush();admin=payload.role!='driver'
        if org.disposition!='active' or (org.inn and db.get(InnRestriction,org.inn)):
            forbidden('Регистрация этой организации приостановлена')
        account=User(org_id=org.id,email=email,name=name,surname=payload.surname.strip(),role=payload.role,password_hash=pass_hash(payload.password),is_admin=admin)
        db.add(account);db.flush()
        if payload.role=='driver':
            placeholder=None
            if inv and inv.kind=='team':
                placeholder=db.scalar(select(Driver).join(RosterMember,RosterMember.driver_id==Driver.id).where(
                    RosterMember.carrier_id==org.id,RosterMember.email==email,RosterMember.active.is_(True),Driver.user_id.is_(None)))
            if placeholder:
                placeholder.user_id=account.id
                placeholder.name=name
                for trip in db.scalars(select(InternalTrip).where(InternalTrip.driver_id==placeholder.id,InternalTrip.status.in_(['waiting_driver','selected']))):
                    note_user(db,account.id,trip.shipment_id,'crew_reserved','График обновлён','Рейс доступен в вашем календаре')
                for q in db.scalars(select(Offer).where(Offer.driver_id==placeholder.id,Offer.status.in_(['waiting_driver','driver_ok','selected']))):
                    note_user(db,account.id,q.shipment_id,'crew_reserved','График обновлён','Рейс доступен в вашем календаре')
            else:db.add(Driver(org_id=org.id,user_id=account.id,name=name))
        fields=[payload.vehicle_kind is not None,bool(payload.vehicle_label),payload.capacity_tons is not None]
        if any(fields):
            if payload.role!='carrier' or not admin or not all(fields): invalid('Для машины заполните тип, обозначение и грузоподъёмность',422)
            db.add(Vehicle(org_id=org.id,label=clean_label(payload.vehicle_label,3),kind=payload.vehicle_kind,capacity=payload.capacity_tons))
        if inv: accept_invitation(db,inv,account)
        recovery_code=mint_recovery_code(db,account.id)
        db.add(WebSession(token_hash=sha_token(token),user_id=account.id,csrf=csrf,expires=datetime.now(timezone.utc)+timedelta(days=7)))
    response.set_cookie('logistik_session',token,httponly=True,samesite='strict',
                        secure=os.getenv('LOGISTIK_SECURE_COOKIE','0')=='1',max_age=7*86400,path='/')
    return {'ok':True,'message':'Кабинет создан. Сохраните резервный код для восстановления пароля.',
            'recoveryCode':recovery_code}

class InviteIn(BaseModel):
    kind: Literal['partner','peer','driver_shipper','team','driver_join','shipper_driver']
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
            'expires':inv.expires.isoformat(),'respondedAt':inv.responded_at.isoformat() if inv.responded_at else None,
            'subject':f'invitation:{inv.id}'}

company_decisions=('partner','peer','driver_join','driver_shipper')

def corporate_recipient(db,inv,user):
    if user['role']=='driver' or inv.kind not in company_decisions or not inv.target_user_id:return None
    target=db.get(User,inv.target_user_id)
    return target if target and target.org_id==user['org_id'] and target.role==user['role'] else None

@app.get('/api/participants')
def list_participants(user=Depends(user_session)):
    with SessionLocal() as db:
        partners=[]
        if user['role'] in ('shipper','carrier'):
            clauses=[Approval.active.is_(True),Approval.shipper_id==user['org_id']] if user['role']=='shipper' else [Approval.active.is_(True),Approval.carrier_id==user['org_id']]
            for a in db.scalars(select(Approval).where(*clauses)):
                org=db.get(Organization,a.carrier_id if user['role']=='shipper' else a.shipper_id)
                partners.append({'id':org.id,'name':org.name,'role':org.kind,'subject':f'organization:{org.id}'})
        team=[]
        if user['role']!='driver':
            team=[{'id':u.id,'name':u.name,'email':u.email,'role':u.role,'active':u.active,'admin':u.is_admin,'subject':f'user:{u.id}'}
                  for u in db.scalars(select(User).where(User.org_id==user['org_id']))]
        invitations=[];archived=[]
        source_scope=ParticipantInvitation.sender_user_id==user['id'] if user['role']=='driver' else ParticipantInvitation.sender_org_id==user['org_id']
        incoming=ParticipantInvitation.target_user_id==user['id']
        if user['role']!='driver':
            members=select(User.id).where(User.org_id==user['org_id'],User.role==user['role'])
            incoming=incoming | (ParticipantInvitation.kind.in_(company_decisions) & ParticipantInvitation.target_user_id.in_(members))
        for inv in db.scalars(select(ParticipantInvitation).where(
                source_scope | incoming).order_by(ParticipantInvitation.id.desc())):
            outgoing=inv.sender_user_id==user['id'] if user['role']=='driver' else inv.sender_org_id==user['org_id']
            if inv.sender_user_id!=user['id'] and (inv.target_user_id==user['id'] or corporate_recipient(db,inv,user)):outgoing=False
            item=invitation_dict(db,inv,outgoing)
            item['canManage']=manages_invitation(inv,user);item['connected']=invitation_connected(db,inv)
            item['archived']=inv.sender_archived if outgoing else inv.recipient_archived
            (archived if item['archived'] else invitations).append(item)
        drivers=[]
        if user['role']=='shipper':
            for relation in db.scalars(select(ShipperDriver).where(ShipperDriver.shipper_id==user['org_id'],ShipperDriver.active.is_(True))):
                target=db.get(User,relation.user_id)
                if target and target.active:drivers.append({'id':target.id,'name':target.name,'email':target.email,'role':'driver','subject':f'user:{target.id}'})
        companies=[]
        if user['role']=='driver':
            org=db.get(Organization,user['org_id'])
            if org.kind=='carrier':companies.append({'id':org.id,'name':org.name,'role':'carrier','currentEmployer':True,'subject':f'organization:{org.id}'})
            driver=db.scalar(select(Driver).where(Driver.user_id==user['id']))
            if driver:
                secondary=db.scalars(select(Organization).join(RosterMember,RosterMember.carrier_id==Organization.id).where(
                    RosterMember.driver_id==driver.id,RosterMember.active.is_(True),Organization.id!=org.id))
                for carrier in secondary:
                    companies.append({'id':carrier.id,'name':carrier.name,'role':'carrier','currentEmployer':False,'subject':f'organization:{carrier.id}'})
            for relation in db.scalars(select(ShipperDriver).where(ShipperDriver.user_id==user['id'],ShipperDriver.active.is_(True))):
                shipper=db.get(Organization,relation.shipper_id)
                companies.append({'id':shipper.id,'name':shipper.name,'role':'shipper','currentEmployer':False,'subject':f'organization:{shipper.id}'})
        notes={n.subject:n.body for n in db.scalars(select(ParticipantComment).where(ParticipantComment.user_id==user['id']))}
        roster=[]
        if user['role']=='carrier':
            for d in carrier_drivers(db,user['org_id']):
                roster.append({'id':d.id,'name':d.name,'canReceive':bool(d.user_id),'subject':f'roster:{d.id}'})
        for collection in (partners,team,drivers,companies,invitations,archived,roster):
            for item in collection:item['comment']=notes.get(item['subject'],'')
        org=db.get(Organization,user['org_id'])
        return {'partners':partners,'roster':roster,'team':team,'invitations':invitations,'archivedInvitations':archived,
                'invitedDrivers':drivers,'companies':companies,'organization':org.name,
                'standaloneDriver':user['role']=='driver' and org.kind=='driver'}

@app.post('/api/participants/invitations',status_code=201)
def invite_participant(payload:InviteIn,request:Request,response:Response,user=Depends(user_session)):
    protect_csrf(request,user['csrf']);email=normalize_email(payload.email)
    if payload.kind=='partner':
        if user['role'] not in ('shipper','carrier') or payload.role!=('carrier' if user['role']=='shipper' else 'shipper'): forbidden('Выберите противоположную роль партнёра')
    elif payload.kind=='peer':
        if user['role'] not in ('shipper','carrier') or payload.role!=user['role']:forbidden()
    elif payload.kind=='driver_shipper':
        if user['role']!='driver' or payload.role!='shipper':forbidden()
    elif payload.kind=='team':
        if not user['is_admin'] or user['role'] not in ('shipper','carrier') or payload.role not in ([user['role'],'driver'] if user['role']=='carrier' else ['shipper']): forbidden('Нет прав добавлять такого сотрудника')
    elif payload.kind=='driver_join':
        if user['role']!='driver' or payload.role!='carrier': forbidden()
    elif payload.kind=='shipper_driver':
        if user['role']!='shipper' or payload.role!='driver':forbidden('Заказчик может пригласить водителя только в свой пул')
    if email==user['email']: invalid('Нельзя пригласить самого себя')
    consume_action('invite:'+str(user['id']),30)
    raw=secrets.token_urlsafe(40)
    with db_write() as db:
        scope=ParticipantInvitation.sender_user_id==user['id'] if user['role']=='driver' else ParticipantInvitation.sender_org_id==user['org_id']
        previous=db.scalar(select(ParticipantInvitation).where(scope,ParticipantInvitation.target_email==email,
                            ParticipantInvitation.kind==payload.kind).order_by(ParticipantInvitation.id.desc()))
        if previous:
            previous.sender_archived=False
            response.status_code=200
            return {'id':previous.id,'alreadyExists':True,'status':previous.status,'emailSent':False,'link':None,
                    'message':'Приглашение уже сохранено в вашем пуле. Повторное уведомление не отправлялось.'}
        target=db.scalar(select(User).where(User.email==email))
        if target and (not target.active or target.role!=payload.role or db.get(Organization,target.org_id).disposition!='active'): invalid('Для этого адреса выбрана неподходящая роль или доступ закрыт')
        if target and target.org_id==user['org_id']: invalid('Участник уже состоит в вашей компании')
        if payload.kind=='team' and target:
            if target.role!='driver' or db.get(Organization,target.org_id).kind!='driver': invalid('Участник уже состоит в другой компании')
        if payload.kind=='driver_join':
            if target and not target.is_admin: invalid('Укажите почту руководителя перевозчика')
        inv=ParticipantInvitation(sender_user_id=user['id'],sender_org_id=user['org_id'],kind=payload.kind,
            target_email=email,target_role=payload.role,target_user_id=target.id if target else None,
            token_hash=sha_token(raw),expires=datetime.now(timezone.utc)+timedelta(days=7))
        db.add(inv);db.flush()
        if target: note_user(db,target.id,None,'participant_invitation','Приглашение к подключению','Откройте «Партнёры», чтобы принять или отклонить приглашение')
        result={'id':inv.id,'link':str(request.base_url).rstrip('/')+'/#invite='+raw,'emailSent':False,'alreadyExists':False}
    return result

@app.post('/api/participants/invitations/{invitation_id}/respond')
def respond_invitation(invitation_id:int,payload:InvitationDecision,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        inv=db.get(ParticipantInvitation,invitation_id);check_invitation(inv)
        # Email alone cannot claim an invitation created before the account existed.
        company_target=corporate_recipient(db,inv,user)
        if inv.target_user_id!=user['id'] and not company_target:
            if inv.target_user_id is not None or not payload.token or not hmac.compare_digest(inv.token_hash,sha_token(payload.token)):
                forbidden('Откройте персональную ссылку приглашения')
        recipient=company_target or db.get(User,user['id'])
        if recipient.email!=inv.target_email or recipient.role!=inv.target_role: forbidden('Это приглашение другому участнику')
        needs_relogin=payload.accepted and inv.kind=='team' and user['role']=='driver'
        if payload.accepted: accept_invitation(db,inv,recipient)
        else: inv.status='rejected';inv.target_user_id=recipient.id;inv.responded_at=datetime.now(timezone.utc)
    return {'ok':True,'relogin':needs_relogin}

@app.post('/api/participants/invitations/{invitation_id}/cancel')
def cancel_invitation(invitation_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        inv=db.get(ParticipantInvitation,invitation_id)
        if not inv or not manages_invitation(inv,user): raise HTTPException(404,detail='Приглашение не найдено')
        check_invitation(inv);inv.status='cancelled';inv.responded_at=datetime.now(timezone.utc)
    return {'ok':True}

@app.post('/api/participants/invitations/{invitation_id}/renew')
def renew_invitation(invitation_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf']);consume_action('invite:'+str(user['id']),30)
    raw=secrets.token_urlsafe(40)
    with db_write() as db:
        inv=db.get(ParticipantInvitation,invitation_id)
        if not inv or not manages_invitation(inv,user): raise HTTPException(404,detail='Приглашение не найдено')
        if inv.status=='accepted' and invitation_connected(db,inv):invalid('Участник уже подключён')
        inv.status='pending';inv.responded_at=None;inv.sender_archived=False;inv.recipient_archived=False
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
    if user['role']!='carrier': forbidden()
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
        if driver and internal_busy(db,driver.id): invalid('Сначала закройте действующие рейсы водителя')
        if driver and db.scalar(select(Offer.id).join(Shipment,Shipment.id==Offer.shipment_id).where(Offer.driver_id==driver.id,
                Offer.status.in_(['waiting_driver','driver_ok','selected']),Shipment.status.notin_(['completed','cancelled']))):
            invalid('Сначала завершите или отмените действующие предложения и рейсы водителя')
        member.active=False;db.execute(delete(WebSession).where(WebSession.user_id==member.id))
    return {'ok':True}

def manages_invitation(inv:ParticipantInvitation,user:dict)->bool:
    if user['role']=='driver':return inv.sender_user_id==user['id']
    return (inv.sender_org_id==user['org_id'] and user['role'] in ('shipper','carrier')
            and (inv.kind!='team' or user['is_admin']))

def invitation_connected(db:Session,inv:ParticipantInvitation)->bool:
    sender=db.get(User,inv.sender_user_id);target=db.get(User,inv.target_user_id) if inv.target_user_id else None
    if not sender or not target:return False
    if inv.kind=='peer':return bool(db.get(CompanyPeer,tuple(sorted((sender.org_id,target.org_id)))))
    if inv.kind=='driver_shipper':return bool(db.scalar(select(ShipperDriver.id).where(ShipperDriver.shipper_id==target.org_id,ShipperDriver.user_id==sender.id,ShipperDriver.active.is_(True))))
    if inv.kind=='partner':
        shipper=sender.org_id if sender.role=='shipper' else target.org_id
        carrier=sender.org_id if sender.role=='carrier' else target.org_id
        return db.scalar(select(Approval.id).where(Approval.shipper_id==shipper,Approval.carrier_id==carrier,Approval.active.is_(True))) is not None
    if inv.kind=='shipper_driver':return db.scalar(select(ShipperDriver.id).where(ShipperDriver.shipper_id==sender.org_id,ShipperDriver.user_id==target.id,ShipperDriver.active.is_(True))) is not None
    if inv.kind=='team':return target.active and target.org_id==inv.sender_org_id
    if inv.kind=='driver_join':
        driver=db.scalar(select(Driver).where(Driver.user_id==sender.id))
        return bool(driver and carrier_has_driver(db,target.org_id,driver))
    return False

def driver_busy(db:Session,driver_id:int)->bool:
    if internal_busy(db,driver_id):return True
    return db.scalar(select(Offer.id).join(Shipment,Shipment.id==Offer.shipment_id).where(
        Offer.driver_id==driver_id,Offer.status.in_(['waiting_driver','driver_ok','selected']),
        Shipment.status.notin_(['completed','cancelled'])).limit(1)) is not None

def disconnect_partner(db:Session,shipper_id:int,carrier_id:int,actor:User):
    approval=db.scalar(select(Approval).where(Approval.shipper_id==shipper_id,Approval.carrier_id==carrier_id,Approval.active.is_(True)))
    if not approval:raise HTTPException(404,detail='Партнёр уже отсутствует в пуле')
    approval.active=False
    db.execute(delete(FleetGrant).where(FleetGrant.carrier_id==carrier_id,FleetGrant.partner_id==shipper_id))
    bids=db.scalars(select(Offer).join(Shipment,Shipment.id==Offer.shipment_id).where(
        Shipment.shipper_id==shipper_id,Offer.carrier_id==carrier_id,Offer.status.in_(['waiting_driver','driver_ok']))).all()
    for bid in bids:
        bid.status='rejected';shipment=db.get(Shipment,bid.shipment_id)
        if shipment.status=='choice' and not db.scalar(select(Offer.id).where(Offer.shipment_id==shipment.id,Offer.id!=bid.id,Offer.status=='driver_ok')):shipment.status='search'
        audit(db,actor,shipment,'carrier_access_revoked',f'carrier={carrier_id};offer={bid.id}')
    for oid,role in ((shipper_id,'shipper'),(carrier_id,'carrier')):
        note_org(db,oid,role,None,'access_revoked','Партнёр удалён из пула','Закрыт доступ к новым заявкам. Назначенные рейсы и история сохранены.')
    return len(bids)

@app.post('/api/participants/organizations/{organization_id}/remove')
def remove_partner(organization_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role'] not in ('shipper','carrier'):forbidden()
    with db_write() as db:
        count=disconnect_partner(db,user['org_id'] if user['role']=='shipper' else organization_id,
                                 user['org_id'] if user['role']=='carrier' else organization_id,db.get(User,user['id']))
    return {'ok':True,'closedPendingOffers':count}

@app.post('/api/participants/drivers/{member_id}/remove')
def remove_invited_driver(member_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='shipper':forbidden()
    with db_write() as db:
        link=db.scalar(select(ShipperDriver).where(ShipperDriver.shipper_id==user['org_id'],ShipperDriver.user_id==member_id,ShipperDriver.active.is_(True)))
        if not link:raise HTTPException(404,detail='Водитель отсутствует в вашем пуле')
        link.active=False
        note_user(db,member_id,None,'pool_removed','Заказчик изменил пул','Вы удалены из пула приглашённых водителей. Ваш кабинет и назначенные рейсы сохранены.')
    return {'ok':True}

@app.post('/api/participants/shippers/{organization_id}/leave')
def leave_shipper_pool(organization_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='driver':forbidden()
    with db_write() as db:
        link=db.scalar(select(ShipperDriver).where(ShipperDriver.shipper_id==organization_id,ShipperDriver.user_id==user['id'],ShipperDriver.active.is_(True)))
        if not link:raise HTTPException(404,detail='Заказчик отсутствует в вашем пуле')
        link.active=False
        note_org(db,organization_id,'shipper',None,'pool_removed','Водитель вышел из пула','Список приглашённых водителей обновлён')
    return {'ok':True}

def detach_driver(db:Session,member:User):
    driver=db.scalar(select(Driver).where(Driver.user_id==member.id))
    if not driver:invalid('Профиль водителя не найден')
    if active_offer_for_carrier(db,driver.id,member.org_id):invalid('Сначала завершите действующие предложения и рейсы водителя')
    old_link=db.scalar(select(RosterMember).where(RosterMember.driver_id==driver.id,RosterMember.carrier_id==member.org_id))
    if old_link:old_link.active=False
    org=Organization(name=member.name,kind='driver');db.add(org);db.flush()
    member.org_id=org.id;driver.org_id=org.id;member.is_admin=False
    db.execute(delete(WebSession).where(WebSession.user_id==member.id))
    note_user(db,member.id,None,'membership_changed','Связь с перевозчиком закрыта','Личный кабинет и история рейсов сохранены. Войдите заново.')

@app.post('/api/participants/membership/leave')
def leave_carrier(request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='driver':forbidden()
    with db_write() as db:
        member=db.get(User,user['id'])
        if db.get(Organization,member.org_id).kind!='carrier':invalid('Вы ещё не подключены к перевозчику')
        old=member.org_id;detach_driver(db,member)
        note_org(db,old,'carrier',None,'membership_changed','Водитель отключён','Водитель вышел из компании; история назначений сохранена')
    return {'ok':True,'relogin':True}

@app.post('/api/carrier/drivers/{driver_id}/unlink')
def unlink_driver(driver_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='carrier':forbidden()
    with db_write() as db:
        driver=db.get(Driver,driver_id)
        if not driver:raise HTTPException(404,detail='Водитель не найден')
        remove_roster_link(db,user['org_id'],driver)
    return {'ok':True}

@app.post('/api/participants/invitations/{invitation_id}/archive')
def archive_invitation(invitation_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        inv=db.get(ParticipantInvitation,invitation_id)
        if not inv:raise HTTPException(404,detail='Приглашение не найдено')
        if manages_invitation(inv,user):
            inv.sender_archived=True
            if inv.status=='pending':inv.status='cancelled';inv.responded_at=datetime.now(timezone.utc)
        elif inv.target_user_id==user['id']:
            inv.recipient_archived=True
            if inv.status=='pending':inv.status='rejected';inv.responded_at=datetime.now(timezone.utc)
        else:forbidden()
    return {'ok':True,'message':'Приглашение убрано из списка. История и комментарии сохранены.'}

@app.post('/api/participants/invitations/{invitation_id}/restore')
def restore_invitation(invitation_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        inv=db.get(ParticipantInvitation,invitation_id)
        if not inv:raise HTTPException(404,detail='Приглашение не найдено')
        if manages_invitation(inv,user):inv.sender_archived=False
        elif inv.target_user_id==user['id']:inv.recipient_archived=False
        else:forbidden()
    return {'ok':True,'message':'Запись восстановлена без повторной отправки приглашения'}

def active_offer_for_carrier(db:Session,driver_id:int,carrier_id:int):
    if internal_busy(db,driver_id,carrier_id):return True
    return db.scalar(select(Offer.id).join(Shipment,Shipment.id==Offer.shipment_id).where(
        Offer.driver_id==driver_id,Offer.carrier_id==carrier_id,
        Offer.status.in_(['waiting_driver','driver_ok','selected']),Shipment.status.notin_(['completed','cancelled'])).limit(1))

def remove_roster_link(db:Session,carrier_id:int,driver:Driver):
    if active_offer_for_carrier(db,driver.id,carrier_id):invalid('Сначала завершите действующий рейс или отзовите предложение')
    link=db.scalar(select(RosterMember).where(RosterMember.carrier_id==carrier_id,RosterMember.driver_id==driver.id,RosterMember.active.is_(True)))
    db.execute(delete(DriverFleetGrant).where(DriverFleetGrant.carrier_id==carrier_id,DriverFleetGrant.driver_id==driver.id))
    if link:link.active=False;link.share_busy=False
    if driver.org_id==carrier_id and driver.user_id:
        detach_driver(db,db.get(User,driver.user_id))
    elif not link:
        raise HTTPException(404,detail='Водитель отсутствует в списке')

@app.post('/api/participants/carriers/{carrier_id}/leave')
def leave_one_carrier(carrier_id:int,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    if user['role']!='driver':forbidden()
    with db_write() as db:
        driver=db.scalar(select(Driver).where(Driver.user_id==user['id']))
        if driver is None:forbidden()
        remove_roster_link(db,carrier_id,driver)
    return {'ok':True}

@app.get('/api/plan')
def weekly_plan(week:date,user=Depends(user_session)):
    """Every row references the original shipment or offer. A view never creates bookings."""
    start=week-timedelta(days=week.weekday())
    end=start+timedelta(days=6)
    return period_plan(start,end,user)

@app.get('/api/plan/month')
def monthly_plan(month:str,user=Depends(user_session)):
    if user['role']!='driver':forbidden()
    if not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])',month):invalid('Укажите месяц в формате ГГГГ-ММ',422)
    try:start=date.fromisoformat(month+'-01')
    except ValueError:invalid('Проверьте дату месяца',422)
    from calendar import monthrange
    return period_plan(start,start.replace(day=monthrange(start.year,start.month)[1]),user)

def period_plan(start:date,end:date,user:dict):
    state=dashboard_state(user,(start,end))
    orders={row['id']:row for row in state['orders']}
    days=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    result={'dates':days,'queue':[],'drivers':[],'inbox':[]}
    if user['role']=='shipper':
        for day in days:
            rows=sorted((x for x in orders.values() if x['date']==day and x['status']!='cancelled'),key=lambda x:x['time'])
            vehicles={q['vehicleId'] for q in state['offers'] if any(x['selectedOffer']==q['id'] for x in rows)}
            result['queue'].append({'date':day,'vehicleCount':len(vehicles),'withoutVehicle':sum(not x['selectedOffer'] for x in rows),'orders':rows})
        return result
    offers=state['offers']
    if user['role']=='carrier':
        result['reviewInbox']=[o['id'] for o in orders.values() if o.get('reviewOnly')]
        result['inbox']=[o['id'] for o in orders.values() if o['status'] in ('search','choice') and not o.get('internal') and not o.get('reviewOnly') and not any(
            q['orderId']==o['id'] and q['status'] in ('waiting_driver','driver_ok','selected') for q in offers)]
    for driver in state['drivers']:
        jobs=[]
        for q in offers:
            order=orders.get(q['orderId'])
            if not order or q['driverId']!=driver['id'] or q['status'] not in ('waiting_driver','driver_ok','selected') or order['status']=='cancelled':continue
            if order['date']>days[-1] or max(order['date'],q['endDate'])<days[0]:continue
            calendar_offer={**q,'day':order['date'],'pickup':order['time'],'bidDay':q['day'],'bidTime':q['pickup']}
            jobs.append({'offer':calendar_offer,'order':order,'canReceive':bool(driver['userId']),'confirmed':q['status']=='selected'})
        for record in orders.values():
            if record.get('internalDriverId')!=driver['id'] or record['unload']<days[0] or record['date']>days[-1]:continue
            if record['privateStage'] not in ('waiting_driver','selected','completed'):continue
            booked=record['privateStage'] in ('selected','completed')
            slot={'id':record['id'],'day':record['date'],'pickup':record['time'],'endDate':record['unload'],'deliveryDay':record['unload'],
                  'status':'selected' if booked else 'waiting_driver','vehicleId':record['internalVehicleId'],'driverId':driver['id']}
            jobs.append({'offer':slot,'order':record,'confirmed':booked,'canReceive':bool(driver['userId']),'internal':True})
        result['drivers'].append({'driver':driver,'jobs':sorted(jobs,key=lambda j:(j['offer']['day'],j['offer']['pickup'],j['offer']['id'])),'busyOutside':[]})
    if user['role']=='carrier' and result['drivers']:
        with SessionLocal() as db:
            own_ids={d.id for d in db.scalars(select(Driver).where(Driver.org_id==user['org_id'],Driver.user_id.is_not(None)))}
            consented=set(db.scalars(select(RosterMember.driver_id).where(RosterMember.carrier_id==user['org_id'],RosterMember.active.is_(True),RosterMember.share_busy.is_(True))))
            shared=own_ids | consented
            outside=db.scalars(select(Offer).join(Shipment,Offer.shipment_id==Shipment.id).where(
                Offer.driver_id.in_(shared),Offer.carrier_id!=user['org_id'],Offer.pickup_date<=end,Offer.end_date>=start,
                Offer.status.in_(['driver_ok','selected']),Shipment.status.notin_(['cancelled','completed']))).all()
            manual_busy=db.execute(select(InternalTrip.driver_id,Shipment.pickup_date,Shipment.unload_date)
                .join(Shipment,InternalTrip.shipment_id==Shipment.id).where(InternalTrip.driver_id.in_(shared),InternalTrip.carrier_id!=user['org_id'],
                InternalTrip.status=='selected',Shipment.pickup_date<=end,Shipment.unload_date>=start)).all()
            for row in result['drivers']:
                row['externalAvailabilityKnown']=row['driver']['id'] in shared
                row['busyOutside']=sorted({day for o in outside if o.driver_id==row['driver']['id'] for day in days if o.pickup_date.isoformat()<=day<=o.end_date.isoformat()} | {day for ident,begin,finish in manual_busy if ident==row['driver']['id'] for day in days if begin.isoformat()<=day<=finish.isoformat()})

    rows=result['drivers']
    if rows:
        readable={r['driver']['id'] for r in rows if user['role']=='driver' or r.get('externalAvailabilityKnown')}
        with SessionLocal() as db:
            leaves=db.scalars(select(DriverLeave).where(DriverLeave.driver_id.in_(readable),DriverLeave.start<=end,DriverLeave.end>=start)
                              .order_by(DriverLeave.start)).all() if readable else []
        for row in rows:
            row['leave']=[leave_dict(x) if user['role']=='driver' else {'start':x.start.isoformat(),'end':x.end.isoformat()}
                          for x in leaves if x.driver_id==row['driver']['id']]
    return result

class CommentIn(BaseModel):
    body:str=Field(max_length=2000)

def can_comment(db:Session,subject:str,user:dict)->bool:
    try:kind,raw=subject.split(':',1);ident=int(raw)
    except (ValueError,TypeError):return False
    if kind=='invitation':
        inv=db.get(ParticipantInvitation,ident)
        return bool(inv and (manages_invitation(inv,user) or inv.target_user_id==user['id'] or (user['role']!='driver' and inv.sender_org_id==user['org_id'])))
    if kind=='roster':
        driver=db.get(Driver,ident)
        return bool(user['role']=='carrier' and driver and carrier_has_driver(db,user['org_id'],driver))
    if kind=='user':
        target=db.get(User,ident)
        return bool(target and (target.org_id==user['org_id'] and user['role']!='driver' or user['role']=='shipper' and db.scalar(select(ShipperDriver.id).where(ShipperDriver.shipper_id==user['org_id'],ShipperDriver.user_id==ident,ShipperDriver.active.is_(True)))))
    if kind=='organization':
        if user['role']=='driver':
            driver=db.scalar(select(Driver).where(Driver.user_id==user['id']))
            return ((driver is not None and carrier_has_driver(db,ident,driver)) or user['org_id']==ident or db.scalar(select(ShipperDriver.id).where(ShipperDriver.shipper_id==ident,ShipperDriver.user_id==user['id'],ShipperDriver.active.is_(True))) is not None)
        if db.get(CompanyPeer,tuple(sorted((user['org_id'],ident)))):return True
        return db.scalar(select(Approval.id).where(Approval.active.is_(True),
            Approval.shipper_id==(user['org_id'] if user['role']=='shipper' else ident),
            Approval.carrier_id==(user['org_id'] if user['role']=='carrier' else ident))) is not None
    return False

@app.post('/api/participants/comments/{subject}')
def save_participant_comment(subject:str,payload:CommentIn,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        if not can_comment(db,subject,user):raise HTTPException(404,detail='Участник не найден в вашем пуле')
        comment=db.scalar(select(ParticipantComment).where(ParticipantComment.user_id==user['id'],ParticipantComment.subject==subject))
        if not payload.body.strip():
            if comment:db.delete(comment)
        elif comment:comment.body=payload.body.strip();comment.updated_at=datetime.now(timezone.utc)
        else:db.add(ParticipantComment(user_id=user['id'],subject=subject,body=payload.body.strip()))
    return {'ok':True}

def normalize_recovery_code(code:str)->str:
    return re.sub(r'[\s-]','',code).upper()

def mint_recovery_code(db:Session,user_id:int)->str:
    now=datetime.now(timezone.utc)
    for old in db.scalars(select(RecoveryCode).where(RecoveryCode.user_id==user_id,RecoveryCode.used_at.is_(None))):old.used_at=now
    raw=secrets.token_hex(16).upper();code='LK-'+'-'.join(raw[i:i+8] for i in range(0,32,8))
    db.add(RecoveryCode(user_id=user_id,code_hash=sha_token(normalize_recovery_code(code))))
    return code

class RecoverySetupIn(BaseModel):
    current_password:str=Field(min_length=1,max_length=128)

class PushKeysIn(BaseModel):
    p256dh:str=Field(min_length=80,max_length=180)
    auth:str=Field(min_length=20,max_length=100)

class PushSubscriptionIn(BaseModel):
    endpoint:str=Field(min_length=20,max_length=2048)
    keys:PushKeysIn

@app.get('/service-worker.js')
def service_worker():
    return FileResponse(BASE/'static'/'service-worker.js',media_type='application/javascript',
                        headers={'Cache-Control':'no-cache','Service-Worker-Allowed':'/','X-Content-Type-Options':'nosniff'})

@app.get('/dispatch31.css')
def dispatch_styles():
    return FileResponse(BASE/'static'/'dispatch31.css',media_type='text/css',headers={'Cache-Control':'no-cache'})

@app.get('/board32.js')
def board_script():
    return FileResponse(BASE/'static'/'board32.js',media_type='application/javascript',headers={'Cache-Control':'no-cache'})

@app.get('/board32.css')
def board_styles():
    return FileResponse(BASE/'static'/'board32.css',media_type='text/css',headers={'Cache-Control':'no-cache'})

@app.get('/dispatch31.js')
def dispatch_interface():
    return FileResponse(BASE/'static'/'dispatch31.js',media_type='application/javascript',headers={'Cache-Control':'no-cache'})

@app.get('/updates29.js')
def interface_updates():
    return FileResponse(BASE/'static'/'updates29.js',media_type='application/javascript',
                        headers={'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'})

@app.post('/api/push/subscribe',status_code=201)
def subscribe_push(payload:PushSubscriptionIn,request:Request,user=Depends(user_session)):
    from push_delivery import public_key,valid_endpoint,valid_keys
    protect_csrf(request,user['csrf'])
    if not public_key():raise HTTPException(503,detail='Фоновые уведомления ещё не настроены')
    if not valid_endpoint(payload.endpoint) or not valid_keys(payload.keys.p256dh,payload.keys.auth):invalid('Некорректная подписка браузера',422)
    consume_action('push-subscribe:'+str(user['id']),30,15)
    key=sha_token(payload.endpoint)
    with db_write() as db:
        current=db.scalar(select(PushSubscription).where(PushSubscription.endpoint_hash==key))
        session_hash=sha_token(request.cookies.get('logistik_session',''))
        active=0
        for device in db.scalars(select(PushSubscription).where(PushSubscription.user_id==user['id'],PushSubscription.active.is_(True))):
            session=db.get(WebSession,device.session_hash)
            if not session or session.expires.replace(tzinfo=timezone.utc)<=datetime.now(timezone.utc):device.active=False
            else:active+=1
        if (not current or not current.active) and active>=10:invalid('Достигнут лимит 10 устройств с уведомлениями')
        if current:
            current.user_id=user['id'];current.p256dh=payload.keys.p256dh;current.auth=payload.keys.auth
            current.session_hash=session_hash;current.active=True
        else:db.add(PushSubscription(user_id=user['id'],endpoint_hash=key,endpoint=payload.endpoint,
                                   p256dh=payload.keys.p256dh,auth=payload.keys.auth,session_hash=session_hash,active=True))
    return {'ok':True}

@app.post('/api/push/disable')
def disable_push(request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf'])
    with db_write() as db:
        for current in db.scalars(select(PushSubscription).where(PushSubscription.user_id==user['id'],
                    PushSubscription.session_hash==sha_token(request.cookies.get('logistik_session','')))):
            current.active=False
    return {'ok':True}

@app.get('/api/account')
def account_info(user=Depends(user_session)):
    from push_delivery import public_key
    with SessionLocal() as db:
        account=db.get(User,user['id']);org=db.get(Organization,user['org_id'])
        ready=db.scalar(select(RecoveryCode.id).where(RecoveryCode.user_id==user['id'],RecoveryCode.used_at.is_(None))) is not None
        return {'name':account.name,'email':account.email,'role':account.role,'organization':org.name,
                'inn':org.inn if account.role in ('shipper','carrier') else None,
                'recoveryCodeReady':ready,'pushEnabled':bool(public_key()),
                'pushDevices':db.scalar(select(func.count()).select_from(PushSubscription).where(PushSubscription.user_id==user['id'],PushSubscription.active.is_(True))) or 0}

@app.post('/api/account/recovery-code')
def setup_recovery(payload:RecoverySetupIn,request:Request,user=Depends(user_session)):
    protect_csrf(request,user['csrf']);consume_action('recovery-setup:'+str(user['id']),8,15)
    with db_write() as db:
        account=db.get(User,user['id'])
        if not pass_ok(payload.current_password,account.password_hash):forbidden('Текущий пароль указан неверно')
        code=mint_recovery_code(db,account.id)
    return {'ok':True,'recoveryCode':code}

class PasswordRecoveryIn(BaseModel):
    email:str=Field(min_length=6,max_length=180)
    recovery_code:str=Field(min_length=10,max_length=160)
    new_password:str=Field(min_length=12,max_length=128)

@app.post('/api/password/recover')
def recover_password(payload:PasswordRecoveryIn,request:Request,response:Response):
    same_origin(request);email=normalize_email(payload.email)
    host=request.client.host if request.client else 'unknown'
    consume_action('recover-ip:'+host,30,15);consume_action('recover-account:'+email,8,15)
    if not payload.new_password.strip():invalid('Пароль не может состоять только из пробелов',422)
    denied=False;new_code=None
    with db_write() as db:
        account=db.scalar(select(User).where(User.email==email,User.active.is_(True)))
        code=db.scalar(select(RecoveryCode).where(RecoveryCode.user_id==account.id,RecoveryCode.used_at.is_(None),
            RecoveryCode.code_hash==sha_token(normalize_recovery_code(payload.recovery_code)))) if account else None
        if not account or not code:denied=True
        else:
            account.password_hash=pass_hash(payload.new_password);code.used_at=datetime.now(timezone.utc)
            db.execute(delete(WebSession).where(WebSession.user_id==account.id))
            db.execute(delete(LoginThrottle).where(LoginThrottle.key==sha_token('account:'+email)))
            new_code=mint_recovery_code(db,account.id)
            note_user(db,account.id,None,'password_recovered','Пароль восстановлен','Все прежние сеансы закрыты. Сохраните новый резервный код.')
    if denied:raise HTTPException(400,detail='Почта или резервный код указаны неверно либо код уже использован')
    response.delete_cookie('logistik_session',path='/')
    return {'ok':True,'recoveryCode':new_code,'message':'Пароль изменён. Сохраните новый код и войдите заново.'}

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

from board_orders import router as orders32
from board_partners import router as partners32
from board_fleet import router as fleet32
from board_admin import router as admin32
for router in (orders32,partners32,fleet32,admin32):
    app.include_router(router)
