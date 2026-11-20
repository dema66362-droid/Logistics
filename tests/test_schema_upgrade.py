"""Upgrade a populated pre-address pilot schema without resetting accounts/orders."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def test_populated_28_schema_upgrades_to_29_without_resetting_identity(tmp_path):
    import hashlib
    database=tmp_path/'legacy28.sqlite3'
    salt=bytes.fromhex('0123456789abcdef0123456789abcdef')
    password=salt.hex()+':'+hashlib.pbkdf2_hmac('sha256',b'Legacy-Schema-Password-2026!',salt,350000).hex()
    with sqlite3.connect(database) as db:
        db.executescript((ROOT/'tests'/'schema_28.sql').read_text())
        db.execute("INSERT INTO organizations (id,name,kind) VALUES (1,'Сохранённая компания','shipper')")
        db.execute("INSERT INTO users (id,org_id,email,name,role,password_hash,active,is_admin) VALUES (1,1,'legacy@example.test','Сохранённый логист','shipper',?,1,1)",(password,))
        db.execute("INSERT INTO schema_migrations (version,applied_at) VALUES ('0028','2026-11-19 00:00:00')")
    env={k:v for k,v in os.environ.items() if not k.startswith('LOGISTIK_')}
    env.update(LOGISTIK_DATABASE_URL='sqlite:///'+str(database),LOGISTIK_DEMO='0')
    code='''from app import *
from fastapi.testclient import TestClient
initialize_schema()
c=TestClient(app)
assert c.post('/api/login',json={'email':'legacy@example.test','password':'Legacy-Schema-Password-2026!'}).status_code==200
s=c.get('/api/state').json()
assert s['user']['id']==1 and s['user']['is_admin'] and s['user']['name']=='Сохранённый логист'
assert c.get('/api/participants').status_code==200
assert c.get('/api/account').json()['recoveryCodeReady'] is False
assert c.get('/health').json()['version']=='3.3.0'
with SessionLocal() as db:
 assert db.get(SchemaMigration,'0029')
 assert 'event_key' in {column['name'] for column in inspect(engine).get_columns('notifications')}
'''
    result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr+result.stdout


def test_old_pilot_columns_are_upgraded_idempotently(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith('LOGISTIK_')}
    env.update(LOGISTIK_DATABASE_URL='sqlite:///' + str(tmp_path / 'old.sqlite3'), LOGISTIK_DEMO='1')
    script = '''from app import engine
with engine.begin() as connection:
 for table, columns in {
  'shipments':['pickup_address','delivery_address','recipient_contact','terms','terms_version','public_external','search_launched','tariff_source'],
  'offers':['terms_accepted','accepted_terms_version'], 'users':['is_admin']}.items():
  for column in columns: connection.exec_driver_sql('ALTER TABLE '+table+' DROP COLUMN '+column)
 connection.exec_driver_sql('DELETE FROM schema_migrations')
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    env['LOGISTIK_DEMO'] = '0'
    script = '''from app import app, initialize_schema
from fastapi.testclient import TestClient
initialize_schema()
c=TestClient(app)
assert c.post('/api/login',json={'email':'shipper@example.test','password':'Demo-2026-Start!'}).status_code==200
state=c.get('/api/state')
assert state.status_code==200, state.text
assert len(state.json()['orders'])==4
assert state.json()['orders'][0]['id']=='P-1048'
assert state.json()['orders'][0]['deliveryAddress']==''
assert state.json()['user']['is_admin'] is True
assert c.post('/api/login',json={'email':'driver@example.test','password':'Demo-2026-Start!'}).status_code==200
assert c.get('/api/state').json()['user']['is_admin'] is False
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
