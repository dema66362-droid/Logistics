"""Independent regression checks for concrete failures found during the 2.7 audit."""
from __future__ import annotations
import csv
from datetime import timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def trial(tmp_path, monkeypatch):
    monkeypatch.setenv('LOGISTIK_DATABASE_URL', 'sqlite:///' + str(tmp_path / 'trial.sqlite3'))
    monkeypatch.setenv('LOGISTIK_DEMO', '1')
    monkeypatch.setenv('LOGISTIK_ALLOW_SIGNUP', '0')
    monkeypatch.setenv('LOGISTIK_SECURE_COOKIE', '0')
    name = 'trial_' + tmp_path.name.replace('-', '_')
    spec = importlib.util.spec_from_file_location(name, ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    module.engine.dispose(); sys.modules.pop(name, None)


def login(module, email='shipper@example.test'):
    client = TestClient(module.app)
    assert client.post('/api/login', json={'email': email, 'password': 'Demo-2026-Start!'}).status_code == 200
    return client, client.get('/api/state').json()['csrf']


def data(module, **changes):
    day = (module.business_today() + timedelta(days=60)).isoformat()
    return {'from_city': 'Тестовый город', 'to_city': 'Тестовая доставка', 'recipient': 'Учебный склад',
            'product': 'Вода техническая', 'tons': '20', 'date': day, 'unload': day, 'time': '09:00',
            'vehicle_type': 'Фура', 'mode': 'Доставка', 'terms': 'Учебные условия',
            'pickup_address': 'Условная площадка 1', 'delivery_address': 'Условный склад 2',
            'recipient_contact': 'Вымышленный контакт', 'budget': 90000, **changes}


def post(client, csrf, path, payload):
    return client.post(path, headers={'X-CSRF-Token': csrf}, json=payload)


@pytest.mark.parametrize('change', [{'from_city': '   '}, {'tons': '0.001'},
                                  {'date': '2001-01-01', 'unload': '2001-01-01'},
                                  {'tons': '100', 'tariff_per_ton': 2000000, 'price_basis': 'per_ton'}])
def test_invalid_inputs_do_not_reach_database(trial, change):
    client, csrf = login(trial)
    response = post(client, csrf, '/api/orders', data(trial, **change))
    assert response.status_code == 422, response.text
    assert client.get('/health').status_code == 200


def test_nonfinite_tariff_query_is_rejected(trial):
    client, _ = login(trial)
    for value in ('nan', 'inf', '-inf'):
        response = client.get('/api/tariff-suggestion', params={'from_city': 'АА', 'to_city': 'ББ', 'tons': value})
        assert response.status_code == 422, response.text


def test_login_throttle_is_durable_and_not_reset_by_another_account(trial):
    client = TestClient(trial.app)
    payload = {'email': 'unknown@example.test', 'password': 'Wrong-local-password'}
    for _ in range(4): assert client.post('/api/login', json=payload).status_code == 401
    login(trial)
    for _ in range(4): assert client.post('/api/login', json=payload).status_code == 401
    # A new process still sees the same failed attempts; memory-only throttles cannot pass.
    script = "from app import app; from fastapi.testclient import TestClient; " + \
             "assert TestClient(app).post('/api/login',json=" + repr(payload) + ").status_code == 429"
    env = {**os.environ, 'LOGISTIK_DEMO': '0'}
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def make_csv(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]), delimiter=';')
    writer.writeheader(); writer.writerows(rows)
    return output.getvalue()


def test_csv_import_is_atomic_private_idempotent_and_role_protected(trial):
    shipper, csrf = login(trial)
    before = len(shipper.get('/api/state').json()['orders'])
    valid = data(trial)
    invalid = data(trial, to_city=' ')
    response = post(shipper, csrf, '/api/import.csv', {'csv': make_csv([valid, invalid])})
    assert response.status_code == 422
    assert len(shipper.get('/api/state').json()['orders']) == before
    payload = {'csv': make_csv([valid, data(trial, to_city='Вторая доставка')])}
    imported = post(shipper, csrf, '/api/import.csv', payload)
    assert imported.status_code == 201, imported.text
    assert imported.json()['created'] == 2
    replay = post(shipper, csrf, '/api/import.csv', payload)
    assert replay.status_code == 200 and replay.json()['created'] == 0
    refs = set(imported.json()['orderIds'])
    carrier, ccsrf = login(trial, 'carrier@example.test')
    assert not refs.intersection(order['id'] for order in carrier.get('/api/state').json()['orders'])
    assert post(carrier, ccsrf, '/api/import.csv', payload).status_code == 403
    assert post(shipper, 'wrong', '/api/import.csv', payload).status_code == 403


def test_backup_restore_and_tamper_protection(tmp_path):
    source, destination = tmp_path / 'source.sqlite3', tmp_path / 'restored.sqlite3'
    snapshot = tmp_path / 'snapshot.json'
    env = {key: value for key, value in os.environ.items() if not key.startswith('LOGISTIK_')}
    env.update(LOGISTIK_DATABASE_URL='sqlite:///' + str(source), LOGISTIK_DEMO='1')
    def run(code, custom_env=env):
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=custom_env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr + result.stdout
    run('from backup_db import save; from pathlib import Path; save(Path(' + repr(str(snapshot)) + '))')
    assert snapshot.stat().st_mode & 0o777 == 0o600
    target_env = {**env, 'LOGISTIK_DATABASE_URL': 'sqlite:///' + str(destination), 'LOGISTIK_DEMO': '0'}
    restore_code = 'from backup_db import restore; from pathlib import Path; restore(Path(' + repr(str(snapshot)) + '),confirm_empty=True)'
    run(restore_code + "; from app import app; from fastapi.testclient import TestClient; c=TestClient(app); "
        "assert c.post('/api/login',json={'email':'shipper@example.test','password':'Demo-2026-Start!'}).status_code==200; "
        "assert len(c.get('/api/state').json()['orders'])==4", target_env)
    # Populated destinations are refused, even when the confirmation flag is present.
    result = subprocess.run([sys.executable, '-c', restore_code], cwd=ROOT, env=target_env, capture_output=True, text=True)
    assert result.returncode != 0 and 'must be empty' in result.stderr
    envelope = json.loads(snapshot.read_text()); envelope['payload'] += ' '
    snapshot.write_text(json.dumps(envelope))
    fresh_env = {**target_env, 'LOGISTIK_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'fresh.sqlite3')}
    result = subprocess.run([sys.executable, '-c', restore_code], cwd=ROOT, env=fresh_env, capture_output=True, text=True)
    assert result.returncode != 0 and 'checksum' in result.stderr
