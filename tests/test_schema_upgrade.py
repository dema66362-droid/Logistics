"""Upgrade a populated pre-address pilot schema without resetting accounts/orders."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_old_pilot_columns_are_upgraded_idempotently(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith('LOGISTIK_')}
    env.update(LOGISTIK_DATABASE_URL='sqlite:///' + str(tmp_path / 'old.sqlite3'), LOGISTIK_DEMO='1')
    script = '''from app import engine
with engine.begin() as connection:
 for table, columns in {
  'shipments':['pickup_address','delivery_address','recipient_contact','terms','terms_version','public_external','search_launched','tariff_source'],
  'offers':['terms_accepted','accepted_terms_version']}.items():
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
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
