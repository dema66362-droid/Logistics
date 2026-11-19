"""Real multi-process HTTP/concurrency/recovery QA on an isolated PostgreSQL schema.

Set LOGISTIK_QA_DATABASE_URL to a dedicated test PostgreSQL. The script creates
two random schemas and removes ONLY those schemas in its finally block.
It never truncates public tables or reuses an application schema.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

import httpx
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_multiuser_http import ACCOUNTS, PASSWORDS, check


def schema_url(url, schema):
    parts = urlsplit(url)
    if any(key == 'options' for key, _ in parse_qsl(parts.query)):
        raise ValueError('Use a dedicated QA URL without PostgreSQL options')
    query = parse_qsl(parts.query) + [('options', '-csearch_path=' + schema)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def run():
    url = os.environ.get('LOGISTIK_QA_DATABASE_URL')
    if not url:
        raise SystemExit('UNVERIFIED: LOGISTIK_QA_DATABASE_URL is required for PostgreSQL QA')
    if url.startswith('postgres://'): url = 'postgresql://' + url[len('postgres://'):]
    if url.startswith('postgresql://'):
        url = 'postgresql+psycopg2://' + url[len('postgresql://'):]
    database = create_engine(url, pool_pre_ping=True)
    schemas = ['logistik_qa_' + uuid.uuid4().hex for _ in range(2)]
    created, processes, clients = [], [], []
    try:
        with database.begin() as connection:
            for schema in schemas:
                connection.execute(text('CREATE SCHEMA ' + schema)); created.append(schema)
        env = {key: value for key, value in os.environ.items() if not key.startswith('LOGISTIK_')}
        env.update(LOGISTIK_DATABASE_URL=schema_url(url, schemas[0]), LOGISTIK_DEMO='0',
                   LOGISTIK_DEPLOYMENT='pilot', LOGISTIK_ALLOW_SIGNUP='1', LOGISTIK_SECURE_COOKIE='0')
        seed = 'from pilot_data import create_pilot_data; create_pilot_data(' + repr(PASSWORDS) + ')'
        def execute(args, current_env=env):
            result = subprocess.run([sys.executable, *args], cwd=ROOT, env=current_env,
                                    capture_output=True, text=True, timeout=45)
            if result.returncode:
                raise AssertionError('QA subprocess failed: ' + result.stderr[-1500:])
            return result
        execute(['-c', seed])
        bases = []
        for _ in range(2):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(port)],
                                       cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(process); base = f'http://127.0.0.1:{port}'; bases.append(base)
            for _ in range(100):
                try:
                    if httpx.get(base + '/health', timeout=.5,trust_env=False).status_code == 200: break
                except httpx.HTTPError: pass
                if process.poll() is not None: raise AssertionError('QA server failed at startup')
                time.sleep(.1)
            else: raise AssertionError('QA server startup timed out')

        def actor(email, server=0):
            client = httpx.Client(base_url=bases[server], timeout=20,trust_env=False); clients.append(client)
            check(client.post('/api/login', json={'email': email, 'password': PASSWORDS[email]}), 200, 'login')
            state = check(client.get('/api/state'), 200, 'initial state')
            return client, state['csrf'], state
        def post(who, path, payload=None):
            return who[0].post(path, json=payload, headers={'X-CSRF-Token': who[1]})
        ship = actor(ACCOUNTS[0][0]); ship2 = actor(ACCOUNTS[0][0], 1)
        carriers = [actor(ACCOUNTS[1][0]), actor(ACCOUNTS[3][0], 1)]
        drivers = [actor(ACCOUNTS[2][0]), actor(ACCOUNTS[4][0], 1)]
        check(post(ship, '/api/orders/TEST-1001/search', {'public_external': False}), 200, 'publish')
        order = ship[2]['orders'][0]
        quotes = []
        for index, carrier in enumerate(carriers):
            state = carrier[0].get('/api/state').json()
            quote = check(post(carrier, '/api/offers', {
                'order_ref': 'TEST-1001', 'driver_id': state['drivers'][0]['id'],
                'vehicle_id': state['vehicles'][0]['id'], 'day': order['date'],
                'pickup': '11:00', 'end_date': order['unload'], 'price': 95000 + index * 1000,
                'accept_terms': True}), 201, 'create proposal')
            quotes.append(int(quote['id'].split('-')[1]))
            check(post(drivers[index], f'/api/offers/{quotes[-1]}/driver-response?accepted=true'), 200, 'driver confirms')
            assert len(carrier[0].get('/api/state').json()['offers']) == 1
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda item: post(item[0], f'/api/offers/{item[1]}/accept'), zip((ship, ship2), quotes)))
        assert sorted(response.status_code for response in responses) == [200, 409], [r.text for r in responses]
        final = ship[0].get('/api/state').json()
        assert final['orders'][0]['status'] == 'confirmed'
        assert sum(quote['status'] == 'selected' for quote in final['offers']) == 1

        # Different orders competing for one driver/vehicle in separate processes.
        day = (date.today() + timedelta(days=30)).isoformat()
        refs = []
        for destination in ('Учебный город Б', 'Учебный город В'):
            result = check(post(ship, '/api/orders', {'from_city': 'Учебный город А', 'to_city': destination,
                'recipient': 'Вымышленный склад', 'product': 'Вода техническая', 'tons': 20,
                'date': day, 'unload': day, 'time': '09:00', 'vehicle_type': 'Фура', 'mode': 'Доставка',
                'budget': 90000, 'terms': 'Учебные условия', 'pickup_address': 'Условная площадка 1',
                'delivery_address': 'Условный склад 2', 'recipient_contact': 'Вымышленный контакт'}), 201, 'new order')
            refs.append(result['id']); check(post(ship, f"/api/orders/{refs[-1]}/search", {'public_external': False}), 200, 'search')
        fleet = carriers[0][0].get('/api/state').json(); held = []
        for ref in refs:
            quote = check(post(carriers[0], '/api/offers', {'order_ref': ref,
                'driver_id': fleet['drivers'][0]['id'], 'vehicle_id': fleet['vehicles'][0]['id'],
                'day': day, 'end_date': day, 'pickup': '10:00', 'price': 95000, 'accept_terms': True}), 201, 'pending bid')
            held.append(int(quote['id'].split('-')[1]))
        other_driver_session = actor(ACCOUNTS[2][0], 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda item: post(item[0], f'/api/offers/{item[1]}/driver-response?accepted=true'),
                                      zip((drivers[0], other_driver_session), held)))
        assert sorted(response.status_code for response in responses) == [200, 409]

        # Login attempts survive changes of worker and successful unrelated logins.
        guest = httpx.Client(base_url=bases[0], timeout=20,trust_env=False); clients.append(guest)
        for index in range(9):
            response = guest.post(bases[index % 2] + '/api/login', json={'email': 'unknown@example.test', 'password': 'Invalid-test-password'})
            assert response.status_code == (401 if index < 8 else 429)
        # New role registration/reciprocal consent uses the same PostgreSQL DB
        # through two independent server processes, including concurrent writes.
        new_actors={};recovery_codes={}
        for index,role in enumerate(('shipper','carrier','driver')):
            c=httpx.Client(base_url=bases[index%2],timeout=20,trust_env=False);clients.append(c)
            payload={'role':role,'email':'new-'+role+'@example.test','name':'Новый '+role,
                     'organization':'Новая '+role,'password':'Isolated-PG-Registration-2026!'}
            registered=check(c.post('/api/signup',json=payload),201,'register '+role)
            recovery_codes[role]=registered['recoveryCode']
            st=check(c.get('/api/state'),200,'new '+role+' session')
            assert st['user']['role']==role
            new_actors[role]=(c,st['csrf'],st)
        new_ship,new_carrier,new_driver=(new_actors[r] for r in ('shipper','carrier','driver'))
        invitation=check(post(new_carrier,'/api/participants/invitations',{
            'kind':'partner','role':'shipper','email':'new-shipper@example.test'}),201,'carrier adds shipper')
        assert new_carrier[0].get('/api/participants').json()['partners']==[]
        other=httpx.Client(base_url=bases[1],timeout=20,trust_env=False);clients.append(other)
        check(other.post('/api/login',json={'email':'new-shipper@example.test','password':'Isolated-PG-Registration-2026!'}),200,'second shipper device')
        other_state=other.get('/api/state').json();second=(other,other_state['csrf'],other_state)
        with ThreadPoolExecutor(max_workers=2) as pool:
            answers=list(pool.map(lambda who:post(who,f"/api/participants/invitations/{invitation['id']}/respond",{'accepted':True}),(new_ship,second)))
        assert sorted(r.status_code for r in answers)==[200,409]
        assert len(new_carrier[0].get('/api/participants').json()['partners'])==1
        reverse=check(post(new_ship,'/api/participants/invitations',{
            'kind':'partner','role':'carrier','email':ACCOUNTS[3][0]}),201,'shipper adds carrier')
        check(post(carriers[1],f"/api/participants/invitations/{reverse['id']}/respond",{'accepted':True}),200,'carrier accepts')
        join=check(post(new_driver,'/api/participants/invitations',{
            'kind':'driver_join','role':'carrier','email':'new-carrier@example.test'}),201,'driver requests carrier')
        check(post(new_carrier,f"/api/participants/invitations/{join['id']}/respond",{'accepted':True}),200,'carrier adds driver')
        assert new_driver[0].get('/api/state').status_code==401
        check(new_driver[0].post('/api/login',json={'email':'new-driver@example.test','password':'Isolated-PG-Registration-2026!'}),200,'driver after membership change')
        assert new_driver[0].get('/api/state').json()['user']['org_id']==new_carrier[2]['user']['org_id']
        # The same invitation is inserted once even across workers/devices.
        invitation_payload={'kind':'shipper_driver','role':'driver','email':'future-driver@example.test'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            repeated=list(pool.map(lambda who:post(who,'/api/participants/invitations',invitation_payload),(new_ship,second)))
        assert sorted(r.status_code for r in repeated)==[200,201]
        assert repeated[0].json()['id']==repeated[1].json()['id']
        subject='invitation:'+str(repeated[0].json()['id'])
        check(post(new_ship,'/api/participants/comments/'+subject,{'body':'Личная заметка на PostgreSQL'}),200,'save comment')
        assert any(i['comment']=='Личная заметка на PostgreSQL' for i in second[0].get('/api/participants').json()['invitations'])
        # Only one concurrent password reset may consume the one-time code.
        recovery_payload={'email':'new-driver@example.test','recovery_code':recovery_codes['driver'],
                          'new_password':'Isolated-PG-Recovered-Password-2026!'}
        guests=[httpx.Client(base_url=base,timeout=20,trust_env=False) for base in bases];clients.extend(guests)
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovered=list(pool.map(lambda client:client.post('/api/password/recover',json=recovery_payload),guests))
        assert sorted(r.status_code for r in recovered)==[200,400]
        assert new_driver[0].get('/api/state').status_code==401
        with tempfile.TemporaryDirectory(prefix='logistik_pg_restore_') as directory:
            snapshot = str(Path(directory) / 'snapshot.json')
            execute(['backup_db.py', 'backup', snapshot])
            recovery_env = {**env, 'LOGISTIK_DATABASE_URL': schema_url(url, schemas[1])}
            execute(['backup_db.py', 'restore', snapshot, '--confirm-empty-database'], recovery_env)
            execute(['-c', "from app import *; from sqlalchemy import select,func; "
                     "db=SessionLocal(); assert db.scalar(select(func.count()).select_from(Shipment))==3; "
                     "assert db.scalar(select(func.count()).select_from(ParticipantInvitation))==4; "
                     "assert db.scalar(select(func.count()).select_from(ParticipantComment))==1; "
                     "assert db.scalar(select(ParticipantInvitation).where(ParticipantInvitation.target_email=='future-driver@example.test')).status=='cancelled'; "
                     "assert db.scalar(select(func.count()).select_from(WebSession))==0; db.close(); "
                     "db=SessionLocal(); x=Organization(name='Проверка последовательности',kind='shipper'); "
                     "db.add(x); db.commit(); assert x.id>3; db.close()"], recovery_env)
        print('PASS: PostgreSQL, 2 server processes, single assignment, exclusive driver hold, durable login throttle, registration 3 roles, reciprocal invitations, concurrent consent, driver membership, single invitation across workers, private comments, single-use concurrent password recovery, pool backup recovery and sequences')
    finally:
        for client in clients: client.close()
        for process in processes:
            process.terminate()
            try: process.wait(timeout=8)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
        with database.begin() as connection:
            for schema in reversed(created): connection.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
        database.dispose()


if __name__ == '__main__': run()
