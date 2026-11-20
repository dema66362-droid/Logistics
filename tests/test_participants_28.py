"""Actual application/database tests: independent role sessions, no mocked API."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, parse_qs

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app as core

PASSWORD='Independent-QA-Password-2026!'

@pytest.fixture
def accounts(tmp_path, monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'roles.sqlite3'),connect_args={'check_same_thread':False})
    core.Base.metadata.create_all(engine)
    monkeypatch.setattr(core,'engine',engine)
    monkeypatch.setattr(core,'SessionLocal',sessionmaker(bind=engine,expire_on_commit=False))
    monkeypatch.setattr(core,'IS_SQLITE',True)
    monkeypatch.setenv('LOGISTIK_ALLOW_SIGNUP','1')
    monkeypatch.setenv('LOGISTIK_SECURE_COOKIE','0')
    def register(role,email=None,**extra):
        client=TestClient(core.app)
        payload=dict(inn="0000000018",role=role,email=email or role+'@example.test',password=PASSWORD,
                     name='Тестовый '+role,organization='Тестовая '+role)
        payload.update(extra)
        response=client.post('/api/signup',json=payload)
        assert response.status_code==201,response.text
        client.qa_recovery_code=response.json().get('recoveryCode')
        assert client.get('/api/state').status_code==200
        return client
    yield register
    engine.dispose()

def state(client): return client.get('/api/state').json()
def post(client,path,body=None):
    return client.post(path,json=body,headers={'X-CSRF-Token':state(client)['csrf']})
def invite(client,email,kind,role):
    r=post(client,'/api/participants/invitations',dict(email=email,kind=kind,role=role))
    assert r.status_code==201,r.text
    assert r.json()['emailSent'] is False
    return r.json()
def token(inv): return parse_qs(urlsplit(inv['link']).fragment)['invite'][0]
def respond(client,inv,accepted=True,**extra):
    return post(client,f"/api/participants/invitations/{inv['id']}/respond",dict(accepted=accepted,**extra))

@pytest.mark.parametrize('role',['shipper','carrier','driver'])
def test_register_login_logout_each_role(accounts,role):
    client=accounts(role)
    data=state(client)
    assert data['user']['role']==role and data['user']['is_admin']==(role!='driver')
    assert data['orders']==[] and data['offers']==[]
    assert post(client,'/api/logout').status_code==200
    assert client.get('/api/state').status_code==401
    assert client.post('/api/login',json=dict(email=' MAILTO:'+role.upper()+'@EXAMPLE.TEST ',password=PASSWORD)).status_code==200
    assert state(client)['user']['role']==role

@pytest.mark.parametrize('from_role,to_role',[('shipper','carrier'),('carrier','shipper')])
def test_reciprocal_partner_invitation_requires_consent(accounts,from_role,to_role):
    sender=accounts(from_role);recipient=accounts(to_role);outsider=accounts(to_role,'stranger@example.test')
    inv=invite(sender,to_role+'@example.test','partner',to_role)
    assert sender.get('/api/participants').json()['partners']==[]
    assert recipient.get('/api/participants').json()['invitations'][0]['id']==inv['id']
    assert outsider.get('/api/participants').json()['invitations']==[]
    assert respond(outsider,inv,token=token(inv)).status_code==403
    assert respond(recipient,inv).status_code==200
    assert len(sender.get('/api/participants').json()['partners'])==1
    assert len(recipient.get('/api/participants').json()['partners'])==1
    assert respond(recipient,inv).status_code==409
    assert not any('token' in key for key in sender.get('/api/participants').json()['invitations'][0])

def test_unregistered_partner_joins_by_personal_link(accounts):
    carrier=accounts('carrier')
    inv=invite(carrier,'new-shipper@example.test','partner','shipper')
    c=TestClient(core.app)
    info=c.post('/api/participants/invitation-info',json={'token':token(inv)})
    assert info.status_code==200 and info.json()['registered'] is False
    payload=dict(inn='0000000018',role='shipper',name='Новый заказчик',organization='Новый заказчик ООО',email='new-shipper@example.test',password=PASSWORD,invitation_token=token(inv))
    assert c.post('/api/signup',json={**payload,'email':'wrong@example.test'}).status_code==403
    assert c.post('/api/signup',json=payload).status_code==201
    assert carrier.get('/api/participants').json()['partners'][0]['name']=='Новый заказчик ООО'
    assert c.post('/api/participants/invitation-info',json={'token':token(inv)}).status_code==409

@pytest.mark.parametrize('company_role,employee_role',[('shipper','shipper'),('carrier','carrier'),('carrier','driver')])
def test_team_registration_and_no_privilege_escalation(accounts,company_role,employee_role):
    owner=accounts(company_role)
    inv=invite(owner,'member@example.test','team',employee_role)
    member=accounts(employee_role,'member@example.test',invitation_token=token(inv),is_admin=True)
    assert state(member)['user']['org_id']==state(owner)['user']['org_id']
    assert state(member)['user']['is_admin'] is False
    assert post(member,'/api/participants/invitations',dict(email='another@example.test',kind='team',role=employee_role)).status_code==403
    if employee_role=='driver':
        assert len(state(owner)['drivers'])==1
        assert member.get('/api/participants').json()['team']==[]
    assert post(member,'/api/carrier/vehicles',dict(label='Новая машина',kind='Фура',capacity=25)).status_code==(201 if employee_role=='carrier' else 403)
    member_id=state(member)['user']['id']
    assert post(owner,f'/api/participants/team/{member_id}/deactivate').status_code==200
    assert member.get('/api/state').status_code==401

def test_new_account_cannot_claim_prior_invitation_by_email_alone(accounts):
    owner=accounts('carrier');inv=invite(owner,'member@example.test','team','driver')
    member=accounts('driver','member@example.test')
    assert member.get('/api/participants').json()['invitations']==[]
    assert respond(member,inv).status_code==403
    assert respond(member,inv,token=token(inv)).status_code==200
    assert member.get('/api/state').status_code==401
    assert member.post('/api/login',json=dict(email='member@example.test',password=PASSWORD)).status_code==200
    assert state(member)['user']['org_id']==state(owner)['user']['org_id']

def test_driver_requests_carrier_and_cannot_be_stolen(accounts):
    carrier=accounts('carrier');driver=accounts('driver');other=accounts('carrier','other@example.test')
    inv=invite(driver,'carrier@example.test','driver_join','carrier')
    assert driver.get('/api/participants').json()['standaloneDriver']
    assert respond(carrier,inv).status_code==200
    assert driver.post('/api/login',json=dict(email='driver@example.test',password=PASSWORD)).status_code==200
    assert state(driver)['user']['org_id']==state(carrier)['user']['org_id']
    assert post(other,'/api/participants/invitations',dict(email='driver@example.test',kind='team',role='driver')).status_code==409
    second=invite(driver,'other@example.test','driver_join','carrier')
    assert respond(other,second).status_code==200
    assert state(driver)['user']['org_id']==state(carrier)['user']['org_id']
    assert len(driver.get('/api/participants').json()['companies'])==2

def test_invitation_lifecycle_expiry_revocation_and_rotation(accounts):
    owner=accounts('shipper');carrier=accounts('carrier');inv=invite(owner,'carrier@example.test','partner','carrier')
    assert post(owner,'/api/participants/invitations',dict(email='carrier@example.test',kind='partner',role='carrier')).status_code==200
    assert post(carrier,f"/api/participants/invitations/{inv['id']}/renew").status_code==404
    new=post(owner,f"/api/participants/invitations/{inv['id']}/renew").json()
    assert TestClient(core.app).post('/api/participants/invitation-info',json={'token':token(inv)}).status_code==404
    with core.db_write() as db: db.get(core.ParticipantInvitation,inv['id']).expires=datetime.now(timezone.utc)-timedelta(seconds=1)
    assert respond(carrier,inv).status_code==409
    assert carrier.get('/api/participants').json()['invitations'][0]['status']=='expired'
    assert post(owner,f"/api/participants/invitations/{inv['id']}/renew").status_code==200
    assert post(owner,f"/api/participants/invitations/{inv['id']}/cancel").status_code==200
    assert respond(carrier,inv).status_code==409
    assert owner.get('/api/participants').json()['partners']==[]

def test_registration_validation_closed_mode_and_csrf(accounts,monkeypatch):
    c=accounts('shipper')
    payload=dict(role='driver',name='Тест',email='new@example.test',password=PASSWORD)
    for changed in ({'email':'bad@@example.test'},{'name':'  '},{'password':'short'},{'role':'admin'},{'email':'bad..name@example.test'}):
        assert TestClient(core.app).post('/api/signup',json={**payload,**changed}).status_code==422
    assert TestClient(core.app).post('/api/signup',json={**payload,'email':' MAILTO:shipper@example.test '}).status_code==409
    assert TestClient(core.app).post('/api/signup',json=payload,headers={'Origin':'https://evil.invalid'}).status_code==403
    assert c.post('/api/participants/invitations',json=dict(email='carrier@example.test',kind='partner',role='carrier')).status_code==403
    monkeypatch.setenv('LOGISTIK_ALLOW_SIGNUP','0')
    assert TestClient(core.app).post('/api/signup',json=payload).status_code==403
    assert TestClient(core.app).get('/api/config').json()['signupEnabled'] is False

def test_vehicle_duplicate_owner_scope_and_capacity(accounts):
    carrier=accounts('carrier');shipper=accounts('shipper');driver=accounts('driver')
    payload=dict(label='Тест А001АА',kind='Фура',capacity=25)
    assert post(carrier,'/api/carrier/vehicles',payload).status_code==201
    assert post(carrier,'/api/carrier/vehicles',payload).status_code==409
    assert post(carrier,'/api/carrier/vehicles',{**payload,'capacity':0}).status_code==422
    assert post(shipper,'/api/carrier/vehicles',payload).status_code==403
    assert post(driver,'/api/carrier/vehicles',payload).status_code==403
    assert len(state(carrier)['vehicles'])==1

def test_new_roles_full_order_cycle_and_commercial_privacy(accounts):
    ship=accounts('shipper');carrier=accounts('carrier');driver=accounts('driver')
    inv=invite(carrier,'shipper@example.test','partner','shipper');assert respond(ship,inv).status_code==200
    inv=invite(driver,'carrier@example.test','driver_join','carrier');assert respond(carrier,inv).status_code==200
    assert driver.post('/api/login',json=dict(email='driver@example.test',password=PASSWORD)).status_code==200
    assert post(carrier,'/api/carrier/vehicles',dict(label='Тест А001АА',kind='Фура',capacity=25)).status_code==201
    day=(core.business_today()+timedelta(days=10)).isoformat()
    payload=dict(from_city='Тест город А',to_city='Тест город Б',recipient='Тестовый склад',product='Вода техническая',tons=20,date=day,time='09:00',unload=day,vehicle_type='Фура',mode='Доставка',budget=100000,terms='Учебные условия',pickup_address='Условная погрузка 1',delivery_address='Условная выгрузка 2',recipient_contact='Тестовый контакт')
    r=post(ship,'/api/orders',payload);assert r.status_code==201,r.text
    ref=r.json()['id'];assert state(carrier)['orders']==[]
    assert post(ship,f'/api/orders/{ref}/search',dict(public_external=False)).status_code==200
    fleet=state(carrier)
    offer=post(carrier,'/api/offers',dict(order_ref=ref,driver_id=fleet['drivers'][0]['id'],vehicle_id=fleet['vehicles'][0]['id'],day=day,pickup='09:00',end_date=day,price=90000,accept_terms=True))
    assert offer.status_code==201,offer.text
    oid=offer.json()['id'].split('-')[1]
    assert all(x['price']==0 for x in state(driver)['offers'])
    assert post(carrier,f'/api/offers/{oid}/driver-response?accepted=true').status_code==403
    assert post(driver,f'/api/offers/{oid}/driver-response?accepted=true').status_code==200
    assert post(carrier,f"/api/participants/team/{state(driver)['user']['id']}/deactivate").status_code==409
    assert post(ship,f'/api/offers/{oid}/accept').status_code==200
    assert state(driver)['orders'][0]['deliveryAddress']=='Условная выгрузка 2'
    assert post(ship,f'/api/orders/{ref}/complete').status_code==200
    assert state(ship)['orders'][0]['status']=='completed'
    assert post(carrier,f"/api/participants/team/{state(driver)['user']['id']}/deactivate").status_code==200

def test_signup_rate_limit_is_durable(accounts):
    accounts('shipper')
    key=core.sha_token('signup:testclient')
    with core.db_write() as db: db.get(core.LoginThrottle,key).failures=30
    r=TestClient(core.app).post('/api/signup',json=dict(role='driver',email='new@example.test',name='Новый водитель',password=PASSWORD))
    assert r.status_code==429

def test_legacy_backup_restores_roles_without_live_invitations(accounts,tmp_path,monkeypatch):
    import hashlib
    import importlib.util
    import json
    from pathlib import Path
    owner=accounts('shipper');accounts('driver')
    invite(owner,'future@example.test','partner','carrier')
    spec=importlib.util.spec_from_file_location('backup_28_test',Path(core.__file__).with_name('backup_db.py'))
    backup=importlib.util.module_from_spec(spec);spec.loader.exec_module(backup)
    path=tmp_path/'snapshot.json';backup.save(path)
    envelope=json.loads(path.read_text());payload=json.loads(envelope['payload'])
    assert len(payload['tables']['participant_invitations'])==1
    for name in backup.ADDITIVE_TABLES:payload['tables'].pop(name,None)
    for row in payload['tables']['notifications']:row.pop('event_key',None)
    # A genuine 2.7 snapshot has no ownership flag; upgrade only that column.
    for row in payload['tables']['users']:row.pop('is_admin')
    envelope['payload']=backup.canonical(payload)
    envelope['sha256']=hashlib.sha256(envelope['payload'].encode()).hexdigest()
    path.write_text(backup.canonical(envelope))
    engine=create_engine('sqlite:///'+str(tmp_path/'recovered.sqlite3'),connect_args={'check_same_thread':False})
    core.Base.metadata.create_all(engine)
    monkeypatch.setattr(core,'engine',engine);monkeypatch.setattr(backup,'engine',engine)
    monkeypatch.setattr(core,'SessionLocal',sessionmaker(bind=engine,expire_on_commit=False))
    try:
        backup.restore(path,confirm_empty=True)
        with core.SessionLocal() as db:
            assert db.scalar(select(core.User).where(core.User.role=='shipper')).is_admin is True
            assert db.scalar(select(core.User).where(core.User.role=='driver')).is_admin is False
            assert db.scalar(select(core.ParticipantInvitation.id)) is None
            assert db.scalar(select(core.WebSession.token_hash)) is None
    finally:engine.dispose()
