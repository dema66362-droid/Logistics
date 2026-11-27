"""Independent tenant / regression checks on an isolated synthetic database.
No actual customer data and only ordinary test cargo.
"""
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import app as a

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module',autouse=True)
def independent_application(tmp_path_factory):
    db_file=tmp_path_factory.mktemp('tenant-audit')/'audit.sqlite3'
    engine=create_engine('sqlite:///'+str(db_file),connect_args={'check_same_thread':False})
    a.Base.metadata.create_all(engine)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(a,'engine',engine)
        patch.setattr(a,'SessionLocal',sessionmaker(bind=engine,expire_on_commit=False))
        patch.setattr(a,'DEMO',True)
        patch.setattr(a,'IS_SQLITE',True)
        patch.setenv('LOGISTIK_ALLOW_SIGNUP','1')
        a.seed_demo()
        yield
    engine.dispose()

PASSWORD = 'Demo-2026-Start!'


def login(email, pw=PASSWORD):
    c = TestClient(a.app)
    assert c.post('/api/login', json={'email': email, 'password': pw}).status_code == 200
    csrf = c.get('/api/state').json()['csrf']
    return c, csrf


def post(c, csrf, path, data=None):
    return c.post(path, json=data, headers={'X-CSRF-Token': csrf})


def order(c, csrf, day, *, budget=100000, to='Новый город', public=False):
    r = post(c, csrf, '/api/orders', {
        'from_city':'Склад А', 'to_city':to, 'pickup_address':'Тестовая площадка',
        'delivery_address':'Тестовый адрес, 25', 'recipient_contact':'Служебный контакт 0000',
        'recipient':'Тестовый получатель', 'product':'Вода техническая',
        'tons':20, 'date':day, 'time':'09:00', 'unload':day,
        'vehicle_type':'Автоцистерна','mode':'Доставка',
        'terms':'Тестовые требования','budget':budget,'public_external':public})
    assert r.status_code == 201, r.text
    return r.json()['id']


def offer(c, csrf, ref, driver, vehicle, day, budget=100000):
    return post(c, csrf, '/api/offers', {
        'order_ref':ref, 'driver_id':driver, 'vehicle_id':vehicle,
        'day':day, 'pickup':'09:00', 'end_date':day,
        'price':budget, 'accept_terms':True})


def add_org(kind, name, email, role):
    with a.db_write() as db:
        org=a.Organization(kind=kind,name=name);db.add(org);db.flush()
        user=a.User(org_id=org.id,email=email,name='Тестовый участник',role=role,
                    password_hash=a.pass_hash(PASSWORD))
        db.add(user);db.flush()
        return org.id, user.id


def test_many_shippers_one_carrier_and_independent_quotes():
    org2, _ = add_org('shipper','Второй независимый отправитель','shipper-b@example.test','shipper')
    ship1,s1=login('shipper@example.test');ship2,s2=login('shipper-b@example.test')
    c1, cc1=login('carrier@example.test');c2,cc2=login('carrier2@example.test')
    with a.db_write() as db:
        # Same carrier (carrier 1) works for two independent customers.
        db.add(a.Approval(shipper_id=org2,carrier_id=2,active=True))
    first=order(ship1,s1,'2027-07-01',to='Первый город')
    second=order(ship2,s2,'2027-07-02',to='Второй город')
    assert post(ship1,s1,f'/api/orders/{first}/search',{'public_external':False}).status_code==200
    assert post(ship2,s2,f'/api/orders/{second}/search',{'public_external':False}).status_code==200
    ids1={r['id'] for r in ship1.get('/api/state').json()['orders']}
    ids2={r['id'] for r in ship2.get('/api/state').json()['orders']}
    assert first in ids1 and second not in ids1
    assert second in ids2 and first not in ids2
    ids_carrier1={r['id'] for r in c1.get('/api/state').json()['orders']}
    ids_carrier2={r['id'] for r in c2.get('/api/state').json()['orders']}
    assert {first,second} <= ids_carrier1
    assert first in ids_carrier2 and second not in ids_carrier2
    assert ship1.get(f'/api/audit/{second}').status_code==403
    assert post(ship1,s1,f'/api/orders/{second}/cancel').status_code==403
    assert post(ship1,s1,'/api/tariff-suggestion',{'x':1}).status_code==405
    csv2=ship2.get('/api/export.csv').text
    assert second in csv2 and first not in csv2
    assert 'Второй независимый отправитель'==ship2.get('/api/state').json()['user']['organizationName']
    assert offer(c1,cc1,second,1,1,'2027-07-02').status_code==201
    assert offer(c2,cc2,second,2,2,'2027-07-02').status_code==403


def test_unfit_approved_carrier_cannot_find_full_opportunities():
    ship2,s2=login('shipper-b@example.test')
    org3,_=add_org('carrier','Перевозчик с недостаточной вместимостью','tiny@example.test','carrier')
    with a.db_write() as db:
        db.add(a.Approval(shipper_id=4,carrier_id=org3,active=True))
        db.add(a.Vehicle(org_id=org3,label='Малая цистерна',kind='Автоцистерна',capacity=5))
    tiny,tc=login('tiny@example.test')
    ref=order(ship2,s2,'2027-07-03',to='Проверка вместимости')
    assert post(ship2,s2,f'/api/orders/{ref}/search',{'public_external':False}).status_code==200
    assert ref not in {r['id'] for r in tiny.get('/api/state').json()['orders']}
    assert not any(x['orderId']==ref for x in tiny.get('/api/state').json()['offers'])


def test_driver_hold_is_released_by_rejection_and_withdrawal():
    ship,ss=login('shipper@example.test');carrier,cc=login('carrier@example.test')
    driver,dd=login('driver@example.test')
    day='2027-07-05'
    first=order(ship,ss,day,budget=100000,to='Направление 1')
    second=order(ship,ss,day,budget=120000,to='Направление 2')
    for ref in (first,second):
        assert post(ship,ss,f'/api/orders/{ref}/search',{'public_external':False}).status_code==200
    proposal1=offer(carrier,cc,first,1,1,day,budget=109000)
    assert proposal1.status_code==201,proposal1.text
    qid=int(proposal1.json()['id'][2:])
    assert post(driver,dd,f'/api/offers/{qid}/driver-response?accepted=true').status_code==200
    # Provisional reservation is visible to the driver and blocks a conflicting offer.
    assert offer(carrier,cc,second,1,1,day,budget=120000).status_code==409
    assert post(ship,ss,f'/api/offers/{qid}/reject').status_code==200
    assert post(ship,ss,f'/api/offers/{qid}/reject').status_code==409
    assert offer(carrier,cc,second,1,1,day,budget=130000).status_code==201
    qid2=max(int(x['id'][2:]) for x in carrier.get('/api/state').json()['offers'] if x['orderId']==second)
    assert post(carrier,cc,f'/api/offers/{qid2}/withdraw').status_code==200
    assert post(carrier,cc,f'/api/offers/{qid2}/withdraw').status_code==409
    assert offer(carrier,cc,first,1,1,day,budget=110000).status_code==201


def test_cancel_notifies_and_scopes_by_organization():
    ship1,s1=login('shipper@example.test');ship2,s2=login('shipper-b@example.test')
    carrier,cc=login('carrier@example.test');driver,dc=login('driver@example.test')
    day='2027-07-07'
    ref=order(ship2,s2,day,to='Отменяемый рейс')
    assert post(ship2,s2,f'/api/orders/{ref}/search',{'public_external':False}).status_code==200
    q=offer(carrier,cc,ref,1,1,day)
    assert q.status_code==201
    qid=int(q.json()['id'][2:])
    assert post(ship1,s1,f'/api/orders/{ref}/cancel').status_code==403
    assert post(ship2,s2,f'/api/orders/{ref}/cancel').status_code==200
    assert post(driver,dc,f'/api/offers/{qid}/driver-response?accepted=true').status_code==409
    statuses={r['id']:r['status'] for r in ship2.get('/api/state').json()['orders']}
    assert statuses[ref]=='cancelled'
    assert ref not in {r['id'] for r in carrier.get('/api/state').json()['orders'] if r['status']!='cancelled'}
    assert ref in {r['ref'] for r in carrier.get('/api/journal-deletions').json()}
    assert any(n['kind']=='order_cancelled' and n['orderId']==ref for n in driver.get('/api/state').json()['notifications'])
    assert post(ship2,s2,f'/api/orders/{ref}/cancel').status_code==200
    assert post(ship2,s2,f'/api/orders/{ref}/search',{'public_external':False}).status_code==409


def test_untrusted_hosts_config_and_pilot_cookies(tmp_path):
    ship,sc=login('shipper@example.test')
    no_cache=ship.get('/api/state')
    assert no_cache.headers['cache-control']=='no-store'
    assert no_cache.headers['x-content-type-options']=='nosniff'
    assert 'logistik_session' in ship.cookies
    assert 'HttpOnly' in ship.post('/api/login',json={'email':'shipper@example.test','password':PASSWORD}).headers['set-cookie']
    env=os.environ.copy();env['LOGISTIK_DEPLOYMENT']='production'
    env['LOGISTIK_DATABASE_URL']='sqlite:///'+str(tmp_path/'unsafe.sqlite3')
    env['LOGISTIK_DEMO']='0';env['LOGISTIK_ALLOW_SIGNUP']='0'
    result=subprocess.run([sys.executable,'-c','import app'],cwd=ROOT,env=env,text=True,capture_output=True)
    assert result.returncode != 0 and 'Unsafe production config' in result.stderr


def test_approved_palette_and_styles_preserved_except_responsive_geometry():
    baseline=(ROOT/'tests'/'approved_style_v2.css').read_text()
    current=(ROOT/'static'/'index.html').read_text()
    def css(text):return text.split('<style>',1)[1].split('</style>',1)[0]
    # Only the measured mobile overflow fix may be appended: no existing rule, color or palette modified.
    addition='@media(max-width:640px){.top{flex-wrap:wrap}.top-actions{max-width:100%;width:100%;flex-wrap:wrap;justify-content:flex-start;gap:6px}.top-actions .btn{flex-shrink:0}}\n'
    assert css(current)==baseline+addition
    assert not any(token in addition for token in ['background:', 'color:', 'border-color:', 'box-shadow:'])
    assert 'data-cancel-order' in current and 'data-withdraw-quote' in current


def test_revoke_one_shipper_preserves_confirmed_trip_and_other_shipper_link():
    ship1,s1=login('shipper@example.test');ship2,s2=login('shipper-b@example.test')
    carrier,c1=login('carrier@example.test');driver,d1=login('driver@example.test')
    ref=order(ship2,s2,'2027-07-11',to='Подтверждённая связь')
    assert post(ship2,s2,f'/api/orders/{ref}/search',{'public_external':False}).status_code==200
    q=offer(carrier,c1,ref,1,1,'2027-07-11')
    assert q.status_code==201,q.text
    id=int(q.json()['id'][2:])
    assert post(driver,d1,f'/api/offers/{id}/driver-response?accepted=true').status_code==200
    assert post(ship2,s2,f'/api/offers/{id}/accept').status_code==200
    assigned=next(x for x in ship2.get('/api/state').json()['orders'] if x['id']==ref)
    assert assigned['status']=='confirmed'
    # Shipper B revokes carrier A; carrier retains own assigned trip, but cannot see new closed jobs.
    rev=post(ship2,s2,'/api/shipper/carriers/2/revoke')
    assert rev.status_code==200,rev.text
    assert post(ship2,s2,'/api/shipper/carriers/2/revoke').status_code==404
    after=next(x for x in carrier.get('/api/state').json()['orders'] if x['id']==ref)
    assert after['deliveryAddress']=='Тестовый адрес, 25'
    assert any(o['id']==ref for o in driver.get('/api/state').json()['orders'])
    next_ref=order(ship2,s2,'2027-07-12',to='Новые ограничения')
    assert post(ship2,s2,f'/api/orders/{next_ref}/search',{'public_external':False}).status_code==200
    assert next_ref not in {x['id'] for x in carrier.get('/api/state').json()['orders']}
    assert offer(carrier,c1,next_ref,1,1,'2027-07-12').status_code==403
    # Revoking B does not revoke A's relationship to a separate customer (shipper 1).
    s1_ref=order(ship1,s1,'2027-07-13',to='Связь с первым заказчиком')
    assert post(ship1,s1,f'/api/orders/{s1_ref}/search',{'public_external':False}).status_code==200
    assert s1_ref in {x['id'] for x in carrier.get('/api/state').json()['orders']}


def test_revoke_closes_pending_bids_and_releases_held_driver():
    ship1,s1=login('shipper@example.test');carrier,c1=login('carrier@example.test')
    driver,d1=login('driver@example.test')
    day='2027-07-15'
    ref=order(ship1,s1,day,budget=100000,to='Связь будет закрыта')
    assert post(ship1,s1,f'/api/orders/{ref}/search',{'public_external':False}).status_code==200
    q=offer(carrier,c1,ref,1,1,day,budget=119000)
    assert q.status_code==201
    qid=int(q.json()['id'][2:])
    assert post(driver,d1,f'/api/offers/{qid}/driver-response?accepted=true').status_code==200
    assert post(ship1,s1,'/api/shipper/carriers/2/revoke').status_code==200
    assert not any(x['orderId']==ref and x['status']=='driver_ok' for x in carrier.get('/api/state').json()['offers'])
    assert any(n['kind']=='offer_rejected' and n['orderId']==ref for n in driver.get('/api/state').json()['notifications'])
    assert ref not in {x['id'] for x in carrier.get('/api/state').json()['orders']}
    assert post(ship1,s1,f'/api/offers/{qid}/accept').status_code==409
