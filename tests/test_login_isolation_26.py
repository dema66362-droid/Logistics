"""Verify independent real accounts, protected data and self-service password reset.

Run isolated in subprocess so it cannot interfere with legacy shared test fixtures.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_five_accounts_have_distinct_passwords_and_sessions_revoke():
    with tempfile.TemporaryDirectory() as tmp:
        env={k:v for k,v in os.environ.items() if not k.startswith('LOGISTIK_')}
        env.update(LOGISTIK_DATABASE_URL='sqlite:///'+str(Path(tmp)/'auth.sqlite3'),
                   LOGISTIK_DEPLOYMENT='pilot',LOGISTIK_DEMO='0',LOGISTIK_ALLOW_SIGNUP='0',
                   LOGISTIK_SECURE_COOKIE='0')
        script='''from app import app
from pilot_data import create_pilot_data
from fastapi.testclient import TestClient
emails=("pilot.shipper@example.test","pilot.carrier1@example.test","pilot.driver1@example.test","pilot.carrier2@example.test","pilot.driver2@example.test")
passwords={email:f"Different-Individual-Test-Password-{i:02d}!" for i,email in enumerate(emails)}
assert create_pilot_data(passwords)
clients=[]
for i,email in enumerate(emails):
 c=TestClient(app)
 assert c.post('/api/login',json=dict(email=email,password=passwords[email])).status_code==200
 assert c.post('/api/login',json=dict(email=email,password=passwords[emails[(i+1)%5]])).status_code==401
 state=c.get('/api/state')
 assert state.status_code==200
 assert state.json()['user']['email']==email
 assert state.json()['user']['role']==('shipper' if i==0 else 'driver' if i in (2,4) else 'carrier')
 clients.append(c)
ship,carrier,driver,carrier2,driver2=clients
assert ship.get('/api/state').json()['orders'][0]['id']=='TEST-1001'
assert carrier.get('/api/state').json()['orders']==[]
assert driver.get('/api/state').json()['orders']==[]
assert carrier2.get('/api/state').json()['orders']==[]
assert driver2.get('/api/state').json()['orders']==[]
# All sessions for this account must be revoked, including another signed-in device.
other=TestClient(app)
assert other.post('/api/login',json=dict(email=emails[0],password=passwords[emails[0]])).status_code==200
csrf=ship.get('/api/state').json()['csrf']
assert ship.post('/api/account/password',json=dict(current_password='wrong',new_password='NewVeryStrongPassword_2026!'),headers={'X-CSRF-Token':csrf}).status_code==403
assert ship.post('/api/account/password',json=dict(current_password=passwords[emails[0]],new_password='NewVeryStrongPassword_2026!')).status_code==403
ok=ship.post('/api/account/password',json=dict(current_password=passwords[emails[0]],new_password='NewVeryStrongPassword_2026!'),headers={'X-CSRF-Token':csrf})
assert ok.status_code==200,ok.text
assert ship.get('/api/state').status_code==401
assert other.get('/api/state').status_code==401
assert ship.post('/api/login',json=dict(email=emails[0],password=passwords[emails[0]])).status_code==401
assert ship.post('/api/login',json=dict(email=emails[0],password='NewVeryStrongPassword_2026!')).status_code==200
assert carrier.get('/api/state').status_code==200
print('five-accounts-password-revocation-ok')'''
        p=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=env,text=True,
                         stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
        assert p.returncode==0,p.stderr+p.stdout
        assert 'five-accounts-password-revocation-ok' in p.stdout

def test_cloud_seeder_refuses_shared_passwords():
    env={k:v for k,v in os.environ.items() if not k.startswith('LOGISTIK_')}
    env.update(LOGISTIK_DEPLOYMENT='pilot',LOGISTIK_DEMO='0',LOGISTIK_ALLOW_SIGNUP='0',
               LOGISTIK_SECURE_COOKIE='1',LOGISTIK_DATABASE_URL='postgresql://example.invalid/test')
    for key in ('SHIPPER','CARRIER1','DRIVER1','CARRIER2','DRIVER2'):
        env['LOGISTIK_TEST_'+key+'_PASSWORD']='SharedExample-NotUnique-2026!'
    p=subprocess.run([sys.executable,'cloud_pilot_seed.py'],cwd=ROOT,env=env,text=True,
                     stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
    assert p.returncode!=0
    assert 'all five passwords must be different' in p.stdout+p.stderr
