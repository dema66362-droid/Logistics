"""Portable, transaction-consistent pilot backup and empty-database recovery.

Snapshots contain password HASHES and business records. Keep them private.
Sessions and login throttles are deliberately excluded from recovery.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path

from sqlalchemy import func, select, text
from app import Base, db_write, engine

EXCLUDED = {'web_sessions', 'login_throttles', 'push_subscriptions', 'push_deliveries', 'site_admin_sessions'}
ADDITIVE_TABLES = {'participant_invitations','shipper_drivers','participant_comments','recovery_codes','dispatch_recipients','carrier_roster','internal_trips','slot_proposals','driver_leave','inn_restrictions','fleet_grants','driver_fleet_grants','carrier_costs','company_peers','road_distances','vehicle_positions'}


def tables():
    return [table for table in Base.metadata.sorted_tables if table.name not in EXCLUDED]


def encode(value):
    if isinstance(value, datetime): return {'$datetime': value.isoformat()}
    if isinstance(value, date): return {'$date': value.isoformat()}
    if isinstance(value, Decimal): return {'$decimal': str(value)}
    raise TypeError('Unsupported snapshot value')


def decode(value):
    if set(value) == {'$datetime'}: return datetime.fromisoformat(value['$datetime'])
    if set(value) == {'$date'}: return date.fromisoformat(value['$date'])
    if set(value) == {'$decimal'}: return Decimal(value['$decimal'])
    return value


def canonical(value):
    return json.dumps(value, default=encode, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def save(path: Path):
    with db_write() as db:
        data = {table.name: [dict(row) for row in db.execute(select(table)).mappings()] for table in tables()}
    payload = canonical({'format': 1, 'tables': data})
    envelope = canonical({'sha256': hashlib.sha256(payload.encode()).hexdigest(), 'payload': payload})
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        file.write(envelope)
        file.flush()
        os.fsync(file.fileno())
    return {name: len(rows) for name, rows in data.items()}


def restore(path: Path, *, confirm_empty: bool):
    if not confirm_empty: raise ValueError('Recovery requires --confirm-empty-database')
    envelope = json.loads(path.read_text(encoding='utf-8'))
    payload = envelope['payload']
    if not hmac.compare_digest(hashlib.sha256(payload.encode()).hexdigest(), envelope['sha256']):
        raise ValueError('Snapshot checksum mismatch')
    snapshot = json.loads(payload, object_hook=decode)
    expected={table.name for table in tables()};present=set(snapshot.get('tables',{}))
    if snapshot.get('format') != 1 or present-expected or not expected-present<=ADDITIVE_TABLES:
        raise ValueError('Incompatible snapshot format or tables')
    for name in expected-present:snapshot['tables'][name]=[]
    historical_offers={row['id']:row for row in snapshot['tables']['offers']}
    for table in tables():
        columns = set(table.columns.keys())
        if table.name == 'users':
            for row in snapshot['tables']['users']:
                row.setdefault('surname','')
                if 'is_admin' not in row:
                    row['is_admin'] = row['role'] in ('shipper', 'carrier')
        if table.name=='organizations':
            for row in snapshot['tables'][table.name]:
                row.setdefault('inn',None);row.setdefault('directory_key',row['name'].casefold().replace('ё','е'));row.setdefault('disposition','active')
        if table.name=='shipments':
            for row in snapshot['tables'][table.name]:
                bid=historical_offers.get(row.get('selected_offer_id')) or {}
                row.setdefault('slot_day',bid.get('pickup_date'))
                row.setdefault('slot_time',bid.get('pickup_time'))
                row.setdefault('creator_id',None);row.setdefault('creator_name',row.get('manager',''))
                row.setdefault('first_price',row.get('budget') or bid.get('price'));row.setdefault('revision',1)
                row.setdefault('price_revised',bool(bid and row['first_price']!=bid.get('price')))
                row.setdefault('delete_after',None)
                row.setdefault('pickup_company','')
                row.setdefault('pickup_contact','')
        if table.name=='slot_proposals':
            for row in snapshot['tables'][table.name]:row.setdefault('new_unload',None)
        if table.name=='offers':
            for row in snapshot['tables'][table.name]:row.setdefault('delivery_day',None)
        if table.name == 'notifications':
            for row in snapshot['tables'][table.name]:
                if set(row)==columns-{'event_key'}:row['event_key']=None
        if table.name=='participant_invitations':
            for row in snapshot['tables'][table.name]:
                if set(row)==columns-{'sender_archived','recipient_archived'}:
                    row.update(sender_archived=False,recipient_archived=False)
        if any(set(row) != columns for row in snapshot['tables'][table.name]):
            raise ValueError('Incompatible snapshot columns')
    with db_write() as db:
        # Refuse even partially occupied databases; never erase existing information.
        if any(db.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables
               if table.name != 'schema_migrations'):
            raise ValueError('Destination database must be empty; nothing was changed')
        for table in tables():
            rows = snapshot['tables'][table.name]
            if table.name=='participant_invitations':
                # Restore the pool/history and private comments, never old access links.
                for row in rows:
                    if row['status']=='pending':
                        row['status']='cancelled';row['expires']=datetime.now().astimezone()
            if table.name == 'schema_migrations':
                # Initialization already inserted this release's migration marker.
                existing = set(db.scalars(select(table.c.version)))
                rows = [row for row in rows if row['version'] not in existing]
            if rows: db.execute(table.insert(), rows)
        if engine.dialect.name == 'postgresql':
            for table in tables():
                for column in table.primary_key:
                    if column.type.python_type is not int: continue
                    sequence = db.scalar(text('SELECT pg_get_serial_sequence(:table, :column)'),
                                         {'table': table.name, 'column': column.name})
                    if sequence:
                        next_id = (db.scalar(select(func.max(column))) or 0) + 1
                        db.execute(text('SELECT setval(CAST(:seq AS regclass), :value, false)'),
                                   {'seq': sequence, 'value': next_id})
    return {name: len(rows) for name, rows in snapshot['tables'].items()}


def main():
    parser = argparse.ArgumentParser(description='Логистик: резервная копия учебной базы')
    sub = parser.add_subparsers(dest='command', required=True)
    backup = sub.add_parser('backup'); backup.add_argument('path', type=Path)
    recovery = sub.add_parser('restore'); recovery.add_argument('path', type=Path)
    recovery.add_argument('--confirm-empty-database', action='store_true')
    args = parser.parse_args()
    try:
        counts = save(args.path) if args.command == 'backup' else restore(args.path, confirm_empty=args.confirm_empty_database)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps({'ok': True, 'rows': counts}, ensure_ascii=False))


if __name__ == '__main__': main()
