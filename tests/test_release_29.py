"""Real DB/API acceptance for accounts, persistent pools and all order parties."""
import base64
from datetime import datetime,timedelta,timezone
import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import app as core
import push_delivery
from test_participants_28 import accounts,post,state,invite,respond,token,PASSWORD

def relogin(client,email,password=PASSWORD):
    r=client.post('/api/login',json={'email':email,'password':password});assert r.status_code==200,r.text

@pytest.mark.parametrize('role',['shipper','carrier','driver'])
def test_recovery_is_single_use_and_revokes_all_devices(accounts,role):
    actor=accounts(role);old_code=actor.qa_recovery_code
    other=TestClient(core.app);relogin(other,role+'@example.test')
    payload={'email':role+'@example.test','recovery_code':old_code,'new_password':'New-Recovered-Password-2026!'}
    guest=TestClient(core.app)
    assert guest.post('/api/password/recover',json={**payload,'recovery_code':'LK-'+'0'*32}).status_code==400
    r=guest.post('/api/password/recover',json=payload);assert r.status_code==200,r.text
    assert r.json()['recoveryCode']!=old_code
    assert actor.get('/api/state').status_code==401 and other.get('/api/state').status_code==401
    assert guest.post('/api/password/recover',json=payload).status_code==400
    assert guest.post('/api/login',json={'email':payload['email'],'password':PASSWORD}).status_code==401
    relogin(guest,payload['email'],payload['new_password'])
    assert state(guest)['user']['role']==role
    with core.SessionLocal() as db:
        rows=db.scalars(select(core.RecoveryCode)).all()
        assert all(row.code_hash not in (old_code,r.json()['recoveryCode']) for row in rows)
        assert sum(row.used_at is None for row in rows)==1

def test_recovery_code_setup_requires_current_password_and_csrf(accounts):
    actor=accounts('shipper');other=accounts('driver')
    assert actor.post('/api/account/recovery-code',json={'current_password':PASSWORD}).status_code==403
    assert post(actor,'/api/account/recovery-code',{'current_password':'incorrect'}).status_code==403
    r=post(actor,'/api/account/recovery-code',{'current_password':PASSWORD});assert r.status_code==200
    assert r.json()['recoveryCode']!=actor.qa_recovery_code
    assert actor.get('/api/account').json()['email']=='shipper@example.test'
    assert other.get('/api/account').json()['role']=='driver'
    data={'email':'shipper@example.test','recovery_code':actor.qa_recovery_code,'new_password':'New-Password-2026!'}
    assert TestClient(core.app).post('/api/password/recover',json=data).status_code==400
    assert TestClient(core.app).post('/api/password/recover',json={**data,'recovery_code':r.json()['recoveryCode']},headers={'Origin':'https://evil.invalid'}).status_code==403

@pytest.mark.parametrize('sender_role,target_role,kind',[
    ('shipper','carrier','partner'),('shipper','driver','shipper_driver'),
    ('carrier','shipper','partner'),('carrier','driver','team'),('driver','carrier','driver_join')])
def test_invite_sent_once_and_restored_from_archive_without_resending(accounts,sender_role,target_role,kind):
    sender=accounts(sender_role);target=accounts(target_role)
    invitation=invite(sender,target_role+'@example.test',kind,target_role)
    with core.SessionLocal() as db:before=db.scalar(select(func.count()).select_from(core.Notification))
    payload={'email':target_role+'@example.test','kind':kind,'role':target_role}
    r=post(sender,'/api/participants/invitations',payload)
    assert r.status_code==200 and r.json()['alreadyExists'] and r.json()['id']==invitation['id']
    with core.SessionLocal() as db:assert db.scalar(select(func.count()).select_from(core.Notification))==before
    assert respond(target,invitation).status_code==200
    for client,role in ((sender,sender_role),(target,target_role)):
        if client.get('/api/state').status_code==401:relogin(client,role+'@example.test')
    with core.SessionLocal() as db:after_accept=db.scalar(select(func.count()).select_from(core.Notification))
    assert post(sender,'/api/participants/invitations',payload).json()['id']==invitation['id']
    assert post(sender,f"/api/participants/invitations/{invitation['id']}/archive").status_code==200
    pool=sender.get('/api/participants').json()
    assert not pool['invitations'] and len(pool['archivedInvitations'])==1
    assert post(sender,f"/api/participants/invitations/{invitation['id']}/restore").status_code==200
    with core.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(core.ParticipantInvitation))==1
        assert db.scalar(select(func.count()).select_from(core.Notification))==after_accept

def test_shipper_invites_new_driver_without_stealing_carrier_membership(accounts):
    shipper=accounts('shipper');carrier=accounts('carrier')
    invitation=invite(shipper,'new-driver@example.test','shipper_driver','driver')
    driver=accounts('driver','new-driver@example.test',invitation_token=token(invitation))
    assert state(driver)['user']['role']=='driver'
    assert state(driver)['user']['org_id'] not in (state(shipper)['user']['org_id'],state(carrier)['user']['org_id'])
    assert shipper.get('/api/participants').json()['invitedDrivers'][0]['id']==state(driver)['user']['id']
    assert driver.get('/api/participants').json()['companies'][0]['role']=='shipper'
    assert post(driver,'/api/participants/organizations/999/remove').status_code==403
    assert post(shipper,f"/api/participants/drivers/{state(driver)['user']['id']}/remove").status_code==200
    assert shipper.get('/api/participants').json()['invitedDrivers']==[]
    assert driver.get('/api/state').status_code==200

def test_private_comments_persist_and_cannot_be_read_or_overwritten_by_colleagues(accounts):
    owner=accounts('shipper');carrier=accounts('carrier')
    team=invite(owner,'employee@example.test','team','shipper')
    employee=accounts('shipper','employee@example.test',invitation_token=token(team))
    invitation=invite(owner,'carrier@example.test','partner','carrier');subject='invitation:'+str(invitation['id'])
    body='Проверен. <script>alert(1)</script> — личная заметка'
    assert post(owner,'/api/participants/comments/'+subject,{'body':body}).status_code==200
    own=owner.get('/api/participants').json();staff=employee.get('/api/participants').json()
    assert next(i for i in own['invitations'] if i['id']==invitation['id'])['comment']==body
    assert next(i for i in staff['invitations'] if i['id']==invitation['id'])['comment']==''
    assert post(employee,'/api/participants/comments/'+subject,{'body':'Заметка сотрудника'}).status_code==200
    assert next(i for i in owner.get('/api/participants').json()['invitations'] if i['id']==invitation['id'])['comment']==body
    outsider=accounts('driver','outsider@example.test')
    assert post(outsider,'/api/participants/comments/'+subject,{'body':'Чужая правка'}).status_code==404
    assert respond(carrier,invitation).status_code==200
    org_subject='organization:'+str(state(carrier)['user']['org_id'])
    assert post(owner,'/api/participants/comments/'+org_subject,{'body':'Договорной перевозчик'}).status_code==200
    assert post(owner,f"/api/participants/invitations/{invitation['id']}/archive").status_code==200
    assert owner.get('/api/participants').json()['archivedInvitations'][0]['comment']==body
    other_device=TestClient(core.app);relogin(other_device,'shipper@example.test')
    assert other_device.get('/api/participants').json()['archivedInvitations'][0]['comment']==body

@pytest.mark.parametrize('who',['shipper','carrier'])
def test_either_company_can_remove_partner(accounts,who):
    shipper=accounts('shipper');carrier=accounts('carrier')
    invitation=invite(carrier,'shipper@example.test','partner','shipper');assert respond(shipper,invitation).status_code==200
    actor,target=(shipper,carrier) if who=='shipper' else (carrier,shipper)
    assert post(actor,f"/api/participants/organizations/{state(target)['user']['org_id']}/remove").status_code==200
    assert actor.get('/api/participants').json()['partners']==[]
    assert target.get('/api/participants').json()['partners']==[]
    assert actor.get('/api/state').status_code==200 and target.get('/api/state').status_code==200

def test_driver_can_leave_customer_pool_without_losing_personal_account(accounts):
    ship=accounts('shipper');driver=accounts('driver');outside=accounts('driver','outside@example.test')
    invitation=invite(ship,'driver@example.test','shipper_driver','driver');assert respond(driver,invitation).status_code==200
    org=state(ship)['user']['org_id']
    assert post(outside,f'/api/participants/shippers/{org}/leave').status_code==404
    assert post(driver,f'/api/participants/shippers/{org}/leave').status_code==200
    assert driver.get('/api/participants').json()['companies']==[]
    assert driver.get('/api/state').status_code==200

def test_carrier_can_unlink_idle_driver_without_deleting_identity(accounts):
    ship,carrier,driver=crew(accounts);other=accounts('carrier','other@example.test')
    did=state(carrier)['drivers'][0]['id'];uid=state(driver)['user']['id']
    assert post(other,f'/api/carrier/drivers/{did}/unlink').status_code==404
    assert post(carrier,f'/api/carrier/drivers/{did}/unlink').status_code==200
    assert driver.get('/api/state').status_code==401
    relogin(driver,'driver@example.test')
    assert state(driver)['user']['id']==uid
    assert driver.get('/api/participants').json()['standaloneDriver']

def crew(accounts):
    ship=accounts('shipper');carrier=accounts('carrier');driver=accounts('driver')
    i=invite(carrier,'shipper@example.test','partner','shipper');assert respond(ship,i).status_code==200
    i=invite(driver,'carrier@example.test','driver_join','carrier');assert respond(carrier,i).status_code==200;relogin(driver,'driver@example.test')
    assert post(carrier,'/api/carrier/vehicles',{'label':'QA 29 Фура','kind':'Фура','capacity':25}).status_code==201
    return ship,carrier,driver

def new_order(ship):
    day=(core.business_today()+timedelta(days=12)).isoformat()
    r=post(ship,'/api/orders',{'from_city':'Учебный город А','to_city':'Учебный город Б','recipient':'Учебный склад',
        'product':'Вода техническая','tons':20,'date':day,'time':'09:00','unload':day,'vehicle_type':'Фура',
        'mode':'Доставка','budget':100000,'terms':'Учебные условия','pickup_address':'Условная площадка 1',
        'delivery_address':'Условный склад 2','recipient_contact':'Учебный контакт'})
    assert r.status_code==201,r.text
    return r.json()['id'],day

def quote(carrier,ref,day):
    s=state(carrier);r=post(carrier,'/api/offers',{'order_ref':ref,'driver_id':s['drivers'][0]['id'],
        'vehicle_id':s['vehicles'][0]['id'],'day':day,'pickup':'09:00','end_date':day,'price':90000,'accept_terms':True})
    assert r.status_code==201,r.text
    return int(r.json()['id'].split('-')[1])

def test_every_order_action_notifies_each_party_once_without_global_broadcast(accounts):
    ship,carrier,driver=crew(accounts);outside=accounts('carrier','outside@example.test')
    ref,day=new_order(ship)
    def count(c):return len([n for n in state(c)['notifications'] if n['orderId']==ref])
    assert [count(c) for c in (ship,carrier,driver,outside)]==[1,0,0,0]
    assert post(ship,f'/api/orders/{ref}/search',{'public_external':False}).status_code==200
    assert [count(c) for c in (ship,carrier,driver,outside)]==[2,1,0,0]
    oid=quote(carrier,ref,day)
    assert [count(c) for c in (ship,carrier,driver,outside)]==[3,2,1,0]
    assert post(driver,f'/api/offers/{oid}/driver-response?accepted=true').status_code==200
    assert [count(c) for c in (ship,carrier,driver,outside)]==[4,3,2,0]
    assert post(carrier,'/api/participants/membership/leave').status_code==403
    assert post(driver,'/api/participants/membership/leave').status_code==409
    assert post(ship,f'/api/offers/{oid}/accept').status_code==200
    assert [count(c) for c in (ship,carrier,driver,outside)]==[5,4,3,0]
    assert post(ship,f'/api/orders/{ref}/complete').status_code==200
    assert [count(c) for c in (ship,carrier,driver,outside)]==[6,5,4,0]
    for c in (ship,carrier,driver):
        for n in state(c)['notifications']:
            if n['orderId']==ref:assert '90000' not in n['message'] and 'Условный склад' not in n['message']
    assert post(driver,'/api/participants/membership/leave').status_code==200
    relogin(driver,'driver@example.test')
    assert driver.get('/api/participants').json()['standaloneDriver']
    assert state(driver)['orders'][0]['status']=='completed'
    i=post(driver,'/api/participants/invitations',{'email':'carrier@example.test','kind':'driver_join','role':'carrier'}).json()
    assert i['alreadyExists']
    assert post(driver,f"/api/participants/invitations/{i['id']}/renew").status_code==200
    assert respond(carrier,i).status_code==200

def test_notification_history_and_read_all_are_private(accounts):
    ship=accounts('shipper');driver=accounts('driver')
    uid=state(ship)['user']['id']
    with core.db_write() as db:
        for index in range(65):core.note_user(db,uid,None,'qa','Учебное уведомление',str(index))
    a=ship.get('/api/notifications').json();assert len(a['notifications'])==50 and a['hasMore']
    b=ship.get('/api/notifications?before='+str(a['notifications'][-1]['id'])).json();assert len(b['notifications'])==15 and not b['hasMore']
    assert driver.get('/api/notifications').json()['notifications']==[]
    assert post(ship,'/api/notifications/read-all').status_code==200
    assert state(ship)['unreadNotifications']==0

@pytest.fixture
def push_config(monkeypatch):
    vapid=ec.generate_private_key(ec.SECP256R1())
    raw=vapid.private_bytes(serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    monkeypatch.setenv('LOGISTIK_VAPID_PRIVATE_KEY',base64.urlsafe_b64encode(raw).decode().rstrip('='))
    monkeypatch.setenv('LOGISTIK_PUBLIC_URL','https://example.test')
    browser=ec.generate_private_key(ec.SECP256R1());point=browser.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
    enc=lambda v:base64.urlsafe_b64encode(v).decode().rstrip('=')
    return {'endpoint':'https://fcm.googleapis.com/fcm/send/synthetic-qa-29','keys':{'p256dh':enc(point),'auth':enc(secrets.token_bytes(16))}}

def test_push_outbox_is_once_per_device_and_never_contains_private_order_data(accounts,push_config):
    actor=accounts('shipper');assert post(actor,'/api/push/subscribe',push_config).status_code==201
    ref,_=new_order(actor)
    sent=[]
    assert push_delivery.deliver_batch(core,lambda subscription,payload:sent.append(payload))==1
    assert len(sent)==1 and sent[0]['title'].startswith('Логистик')
    assert 'Условный' not in str(sent[0]) and '100000' not in str(sent[0])
    assert push_delivery.deliver_batch(core,lambda *args:sent.append(args))==0
    assert len(sent)==1
    assert post(actor,f'/api/orders/{ref}/cancel').status_code==200
    assert post(actor,'/api/logout').status_code==200
    push_delivery.deliver_batch(core,lambda *args:sent.append(args))
    assert len(sent)==1

def test_push_binding_cannot_deliver_previous_accounts_messages(accounts,push_config):
    a=accounts('shipper');b=accounts('carrier')
    assert post(a,'/api/push/subscribe',push_config).status_code==201
    new_order(a)
    assert post(b,'/api/push/subscribe',push_config).status_code==201
    sent=[];push_delivery.deliver_batch(core,lambda *args:sent.append(args));assert not sent
    with core.SessionLocal() as db:assert db.scalar(select(core.PushDelivery)).status=='discarded'

@pytest.mark.parametrize('endpoint',['http://127.0.0.1/a','https://localhost/a','https://169.254.169.254/latest/','https://fcm.googleapis.com.evil.invalid/a','https://evil.invalid/a','https://user:password@fcm.googleapis.com/a'])
def test_push_subscriptions_cannot_be_used_for_ssrf(accounts,push_config,endpoint):
    actor=accounts('shipper')
    assert post(actor,'/api/push/subscribe',{**push_config,'endpoint':endpoint}).status_code==422

def test_push_transient_error_retries_and_expired_device_is_disabled(accounts,push_config):
    actor=accounts('shipper');assert post(actor,'/api/push/subscribe',push_config).status_code==201
    new_order(actor)
    class ProviderError(Exception):status_code=503
    def failure(*args):raise ProviderError()
    push_delivery.deliver_batch(core,failure)
    with core.db_write() as db:
        row=db.scalar(select(core.PushDelivery));assert row.status=='retry' and row.attempts==1
        row.next_attempt=datetime.now(timezone.utc)-timedelta(seconds=1)
    ProviderError.status_code=410;push_delivery.deliver_batch(core,failure)
    with core.SessionLocal() as db:
        assert db.scalar(select(core.PushDelivery)).status=='failed'
        assert db.scalar(select(core.PushSubscription)).active is False

def test_real_webpush_encryption_and_vapid_signing_are_accepted(push_config,monkeypatch):
    import http_ece
    import json
    import pywebpush
    import requests
    receiver=ec.generate_private_key(ec.SECP256R1());auth=secrets.token_bytes(16)
    enc=lambda b:base64.urlsafe_b64encode(b).decode().rstrip('=')
    info={'endpoint':push_config['endpoint'],'keys':{'auth':enc(auth),'p256dh':enc(receiver.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint))}}
    captured={}
    class Transport:
        def post(self,url,**kwargs):
            captured.update(kwargs);response=requests.Response();response.status_code=201;return response
    original=pywebpush.webpush
    monkeypatch.setattr(pywebpush,'webpush',lambda **kwargs:original(requests_session=Transport(),**kwargs))
    data={'title':'Учебное уведомление','notificationId':123,'userId':1}
    push_delivery.send(info,data)
    assert 'vapid' in captured['headers']['Authorization'].lower()
    plain=http_ece.decrypt(captured['data'],private_key=receiver,auth_secret=auth,version='aes128gcm')
    assert json.loads(plain)==data
