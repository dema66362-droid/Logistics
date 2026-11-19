"""Configuration and bootstrap guardrails for a CLOSED trial environment."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def pilot_env(db_path):
    return dict(os.environ, LOGISTIK_DATABASE_URL='sqlite:///'+str(db_path),
                LOGISTIK_DEPLOYMENT='pilot', LOGISTIK_DEMO='0',
                LOGISTIK_ALLOW_SIGNUP='0',LOGISTIK_SECURE_COOKIE='1',
                LOGISTIK_ALLOWED_HOSTS='localhost,127.0.0.1,*.ts.net')


def test_bootstrap_uses_unique_random_passwords_and_refuses_repeat():
    with tempfile.TemporaryDirectory() as td:
        env=pilot_env(Path(td)/'test.sqlite3')
        first=subprocess.run([sys.executable,'bootstrap_closed_test.py'],cwd=ROOT,env=env,
                             capture_output=True,text=True,timeout=25)
        assert first.returncode==0,first.stderr
        assert first.stdout.count('пароль:')==5
        pw=[x.split('пароль: ',1)[1] for x in first.stdout.splitlines() if 'пароль: ' in x]
        assert len(pw)==5 and len(set(pw))==5 and all(len(x)>=18 for x in pw)
        repeated=subprocess.run([sys.executable,'bootstrap_closed_test.py'],cwd=ROOT,env=env,
                             capture_output=True,text=True,timeout=25)
        assert repeated.returncode!=0
        assert 'already has accounts' in repeated.stderr


def test_production_mode_refused_explicitly():
    with tempfile.TemporaryDirectory() as td:
        env=pilot_env(Path(td)/'fail.sqlite3')
        env['LOGISTIK_DEPLOYMENT']='production'
        result=subprocess.run([sys.executable,'-c','import app'],cwd=ROOT,env=env,
                              capture_output=True,text=True,timeout=15)
        assert result.returncode!=0
        assert 'Unsafe production config' in result.stderr
        assert not (Path(td)/'fail.sqlite3').exists()


def test_secure_cookie_and_host_restrictions():
    with tempfile.TemporaryDirectory() as td:
        env=pilot_env(Path(td)/'test.sqlite3')
        env['LOGISTIK_DEMO']='1'
        env['LOGISTIK_ALLOWED_HOSTS']='pilot.example.ts.net,localhost,127.0.0.1'
        program = '''from fastapi.testclient import TestClient
from app import app
with TestClient(app,base_url='https://pilot.example.ts.net') as c:
    r=c.post('/api/login',json={'email':'shipper@example.test','password':'Demo-2026-Start!'})
    assert r.status_code==200,r.text
    assert 'secure' in r.headers['set-cookie'].lower()
    assert 'httponly' in r.headers['set-cookie'].lower()
    assert c.get('/api/state').status_code==200
with TestClient(app,base_url='https://evil.test') as c:
    assert c.get('/health').status_code==400
'''
        result=subprocess.run([sys.executable,'-c',program],cwd=ROOT,env=env,
                              capture_output=True,text=True,timeout=20)
        assert result.returncode==0,result.stderr
