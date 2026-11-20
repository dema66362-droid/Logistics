"""Acceptance checks using real application sessions and isolated synthetic companies."""
from datetime import datetime, timedelta, timezone
import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
import app as c
from board_fleet import distance
from board_orders import cleanup
from test_participants_28 import accounts, PASSWORD, post, respond, state
from test_dispatch_plan import link, members, vehicle, publish, create_bid, internal_payload
from test_release_29 import new_order


def employee(owner,surname):
    address=surname.lower().encode().hex()+'@example.test'
    result=post(owner,'/api/employees',{'surname':surname,'given_name':'Тест','email':address})
    assert result.status_code==201,result.text
    client=TestClient(c.app)
    assert client.post('/api/login',json={'email':address,'password':result.json()['initialPassword']}).status_code==200
    assert state(client)['user']['org_id']==state(owner)['user']['org_id']
    return client


def change(client,ref,**changes):
    r=client.get('/api/journal/'+ref+'/editor')
    assert r.status_code==200,r.text
    return client.put('/api/journal/'+ref,json={**r.json()['order'],**changes},headers={'X-CSRF-Token':state(client)['csrf']})


def assigned(accounts):
    ship,car,driver=accounts('shipper'),accounts('carrier'),accounts('driver')
    link(ship,car,'carrier@example.test')
    person=members(car,'Иванов','driver@example.test');vid=vehicle(car)
    author=employee(ship,'Смирнова')
    ref,day=new_order(author)
    assert publish(ship,ref,car).status_code==200
    q=create_bid(car,ref,person['id'],vid,day,time='09:00')
    assert q.status_code==201,q.text
    assert post(ship,'/api/offers/'+q.json()['id'][2:]+'/accept').status_code==200
    return ship,car,driver,author,ref,day


def test_partner_search_both_directions_company_staff_and_duplicates(accounts):
    ship=accounts('shipper');car=accounts('carrier',organization='ООО ЁжТрак');other=accounts('carrier','other@example.test')
    worker=employee(car,'Лебедев')
    assert ship.get('/api/partners/search',params={'q':'ежт'}).json()==[]
    row=ship.get('/api/partners/search',params={'q':'ЕЖТР'}).json()[0]
    assert row['id']==state(car)['user']['org_id']
    assert row in ship.get('/api/partners/search',params={'q':'0000'}).json()
    inv=post(ship,'/api/partners/invite',{'company_id':row['id']}).json()
    assert post(ship,'/api/partners/invite',{'company_id':row['id']}).json()['alreadyExists']
    assert not ship.get('/api/partners').json()['partners']
    assert worker.get('/api/partners').json()['invitations'][0]['id']==inv['id']
    assert respond(other,inv).status_code==403
    assert respond(worker,inv).status_code==200
    assert ship.get('/api/partners').json()['partners'][0]['name']=='ООО ЁжТрак'
    assert post(worker,'/api/employees',{'surname':'Петров','email':'x@example.test'}).status_code==403
    invited=post(other,'/api/partners/invite',{'company_id':state(ship)['user']['org_id']}).json()
    assert respond(ship,invited,False).status_code==200
    assert not other.get('/api/partners').json()['partners']
    party=worker.get('/api/partners').json()['partners'][0]
    assert post(worker,party['remove']).status_code==200
    assert not ship.get('/api/partners').json()['partners']


def test_shared_edit_first_price_across_roles_author_and_stale_write(accounts):
    ship,car,driver,author,ref,day=assigned(accounts)
    outsider=accounts('driver','unrelated@example.test')
    assert outsider.get('/api/journal/'+ref+'/editor').status_code==404
    stale=car.get('/api/journal/'+ref+'/editor').json()['order']
    for actor,price in ((author,91000),(car,88700),(driver,95000),(ship,100000)):
        r=change(actor,ref,budget=price,manager='Работа смены',note='Общее уточнение')
        assert r.status_code==200,r.text
        view=ship.get('/api/journal/'+ref+'/editor').json()
        assert (view['firstPrice'],view['currentPrice'],view['priceChanged'],view['author'])==(100000,price,True,'Смирнова')
    assert car.put('/api/journal/'+ref,json=stale,headers={'X-CSRF-Token':state(car)['csrf']}).status_code==409
    assert change(driver,ref,time='10:35',recipient_contact='Новый контакт').json()['pendingSlot']
    before=state(ship)['orders'][0]
    assert before['time']=='09:00' and before['slotProposals'][0]['time']=='10:35'
    assert post(author,'/api/orders/'+ref+'/slot/'+str(before['slotProposals'][0]['id']),{'accepted':True}).status_code==200
    assert all(state(x)['orders'][0]['time']=='10:35' for x in (ship,driver,car,author))
    assert driver.get('/api/plan/month',params={'month':day[:7]}).json()['drivers'][0]['jobs'][0]['confirmed']


def test_cancellation_is_idempotent_notifies_three_roles_and_expires(accounts):
    ship,car,driver,worker,ref,day=assigned(accounts)
    now=datetime.now(timezone.utc)
    initial=post(driver,'/api/orders/'+ref+'/cancel')
    assert initial.status_code==200,initial.text
    due=datetime.fromisoformat(initial.json()['deleteAfter']).replace(tzinfo=timezone.utc)
    assert abs((due-now-timedelta(days=1)).total_seconds())<10
    assert post(car,'/api/orders/'+ref+'/cancel').status_code==200
    for actor in (driver,ship,car,worker):
        assert len([n for n in state(actor)['notifications'] if n['kind']=='order_cancelled' and n['orderId']==ref])==1
        assert actor.get('/api/journal-deletions').json()[0]['ref']==ref
    cleanup(due-timedelta(seconds=1))
    with c.SessionLocal() as db:assert db.scalar(select(c.Shipment.id).where(c.Shipment.ref==ref))
    cleanup(due+timedelta(seconds=1))
    assert all(a.get('/api/journal-deletions').json()==[] for a in (ship,driver,car,worker))
    assert all(a.get('/api/journal/'+ref+'/editor').status_code==404 for a in (ship,car,driver))


def test_fleet_share_carrier_costs_driver_privacy_and_revocation(accounts):
    ship,car,driver,worker,ref,day=assigned(accounts)
    oid=state(ship)['user']['org_id'];cid=state(car)['user']['org_id'];did=state(car)['drivers'][0]['id']
    outside=accounts('shipper','outsider@example.test')
    path='/api/fleet/shared/'+str(cid)+'?week='+day
    for actor in (ship,driver,outside):assert actor.get(path).status_code==403
    for party,ident,actor in (('company',oid,ship),('driver',did,driver)):
        assert post(car,'/api/fleet/share',{'partner_id':ident,'subject':party,'enabled':True}).status_code==200
        rows=actor.get(path).json()
        assert len(rows)==1 and set(rows[0])=={'vehicle','start','end','from','to'}
        assert actor.get('/api/partners').json()['partners'][0]['canViewFleet']
        assert post(car,'/api/fleet/share',{'partner_id':ident,'subject':party,'enabled':False}).status_code==200
        assert actor.get(path).status_code==403
    assert outside.get(path).status_code==403
    assert ship.get('/api/fleet/settings').status_code==403


def test_backhaul_real_money_includes_empty_distance_and_dates(accounts):
    ship=accounts('shipper');other=accounts('shipper','second@example.test');car=accounts('carrier')
    link(ship,car,'carrier@example.test');link(other,car,'carrier@example.test');vid=vehicle(car)
    ref,day=new_order(ship);other_ref,_=new_order(other)
    for customer,shipment in ((ship,ref),(other,other_ref)):
        assert change(customer,shipment,from_city='Уфа',to_city='Орск').status_code==200
        assert publish(customer,shipment,car).status_code==200
    for origin,dest,km in [('Оренбург','Уфа',50),('Уфа','Орск',300),('Оренбург','Орск',210)]:
        assert post(car,'/api/fleet/distance',{'origin':origin,'destination':dest,'km':km}).status_code==200
    assert post(car,'/api/fleet/position',{'vehicle_id':vid,'city':'Оренбург','home':'Орск','available':day}).status_code==200
    assert post(car,'/api/fleet/costs',{'empty_km':35,'loaded_km':45,'wait_day':2000}).status_code==200
    result=car.get('/api/fleet/matches')
    assert result.status_code==200,result.text
    rows=result.json()['items']
    assert {r['order'] for r in rows}=={ref,other_ref}
    assert all((r['emptyKm'],r['margin'],r['gain'])==(50,84750,92100) for r in rows)
    assert not ship.get('/api/fleet/matches').json()['items']
    assert post(car,'/api/fleet/share',{'partner_id':state(ship)['user']['org_id'],'enabled':True}).status_code==200
    show=ship.get('/api/fleet/matches').json()['items']
    assert len(show)==1 and show[0]['order']==ref and show[0]['mutual'] and 'margin' not in show[0] and 'gain' not in show[0]
    assert distance('Несуществующий город','Орск',{})[0] is None
    assert distance('Орск','Бобруйск',{})[0]>1000


def test_global_operator_block_revoke_unblock_and_delete(accounts,monkeypatch):
    ship=accounts('shipper');employee(ship,'Крылов')
    password=secrets.token_urlsafe(32);monkeypatch.setenv('LOGISTIK_SITE_ADMIN_PASSWORD',password)
    client=TestClient(c.app);path='/api/site-admin/company'
    assert ship.get(path,params={'inn':'0000000018'}).status_code==401
    login=client.post('/api/site-admin/login',json={'login':'admin','password':password})
    assert login.status_code==200,login.text
    csrf={'X-CSRF-Token':login.json()['csrf']}
    assert client.get(path,params={'inn':'0000000018'}).json()['companies'][0]['state']=='active'
    assert client.post(path,json={'inn':'0000000018','action':'block'}).status_code==403
    for action in ('block','unblock','delete'):
        assert client.post(path,json={'inn':'0000000018','action':action},headers=csrf).status_code==200
        assert ship.post('/api/login',json={'email':'shipper@example.test','password':PASSWORD}).status_code==(200 if action=='unblock' else 401)
    assert client.post(path,json={'inn':'0000000018','action':'unblock'},headers=csrf).status_code==409


def test_crossed_invitations_keep_one_request_and_previous_terms_need_new_offer(accounts):
    ship,car,driver=accounts('shipper'),accounts('carrier'),accounts('driver')
    carid=state(car)['user']['org_id'];shipid=state(ship)['user']['org_id']
    outgoing=post(ship,'/api/partners/invite',{'company_id':carid}).json()
    reverse=post(car,'/api/partners/invite',{'company_id':shipid}).json()
    assert reverse['alreadyExists'] and reverse['id']==outgoing['id']
    assert len(car.get('/api/partners').json()['invitations'])==1
    assert respond(car,outgoing).status_code==200
    person=members(car,'Водитель','driver@example.test');vid=vehicle(car)
    ref,day=new_order(ship);assert publish(ship,ref,car).status_code==200
    q=create_bid(car,ref,person['id'],vid,day,time='09:00').json()['id'][2:]
    assert change(ship,ref,terms='Новые условия погрузки').status_code==200
    assert post(ship,'/api/offers/'+q+'/accept').status_code==409
    assert create_bid(car,ref,person['id'],vid,day,time='09:00').status_code==409
    version=next(o['termsVersion'] for o in state(car)['orders'] if o['id']==ref)
    assert create_bid(car,ref,person['id'],vid,day,time='09:00',terms_version=version).status_code==201


def test_copy_pending_invitation_preserves_previous_links_and_single_event(accounts):
    ship=accounts('shipper')
    driver=accounts('driver')
    created=post(ship,'/api/participants/invitations',{'kind':'shipper_driver','role':'driver','email':'driver@example.test'}).json()
    path='/api/participants/invitations/'+str(created['id'])
    copies=[post(ship,path+'/link').json()['link'] for _ in range(2)]
    anonymous=TestClient(c.app)
    for url in [created['link'],*copies]:
        assert anonymous.post('/api/participants/invitation-info',json={'token':url.split('#invite=')[1]}).status_code==200
    assert len(driver.get('/api/partners').json()['invitations'])==1
    assert len([n for n in state(driver)['notifications'] if n['kind']=='participant_invitation'])==1
    assert post(driver,path+'/link').status_code==404
    assert post(ship,path+'/cancel').status_code==200
    assert post(ship,path+'/renew').status_code==200
    for url in [created['link'],*copies]:
        assert anonymous.post('/api/participants/invitation-info',json={'token':url.split('#invite=')[1]}).status_code==404


def test_operator_initial_access_history_and_stale_decisions(accounts,monkeypatch):
    ship=accounts('shipper');driver=accounts('driver')
    initial=secrets.token_urlsafe(28);personal=secrets.token_urlsafe(26)
    expiry=datetime.now(timezone.utc)+timedelta(hours=1)
    monkeypatch.setenv('LOGISTIK_SITE_ADMIN_PASSWORD',secrets.token_urlsafe(34))
    monkeypatch.setenv('LOGISTIK_SITE_ADMIN_SETUP_HASH',c.sha_token(initial))
    monkeypatch.setenv('LOGISTIK_SITE_ADMIN_SETUP_UNTIL',expiry.isoformat())
    operator=TestClient(c.app);root='/api/site-admin/'
    form={'login':'admin','password':initial}
    assert operator.post(root+'login',json=form).json()=={'choosePassword':True}
    assert operator.get(root+'company',params={'inn':'0000000018'}).status_code==401
    assert operator.post(root+'setup',json={**form,'new_password':initial}).status_code==422
    result=operator.post(root+'setup',json={**form,'new_password':personal})
    assert result.status_code==200
    headers={'X-CSRF-Token':result.json()['csrf']}
    decision={'inn':'0000000018','action':'block','expected':'active'}
    assert operator.post(root+'company',json=decision,headers=headers).status_code==200
    assert operator.post(root+'company',json=decision,headers=headers).status_code==409
    assert operator.post(root+'company',json={**decision,'expected':'blocked'},headers=headers).status_code==200
    events=operator.get(root+'history').json()
    assert len(events)==1 and (events[0]['before'],events[0]['after'],events[0]['actor'])==('active','blocked','admin')
    assert driver.get(root+'history').status_code==401
    assert operator.post(root+'logout',json={},headers=headers).status_code==200
    assert operator.get(root+'session').status_code==401
    assert operator.post(root+'login',json=form).status_code==401
    assert operator.post(root+'setup',json={**form,'new_password':personal}).status_code==401
    assert operator.post(root+'login',json={'login':'admin','password':personal}).status_code==200
    monkeypatch.setenv('LOGISTIK_SITE_ADMIN_SETUP_HASH',c.sha_token('another-initial-test'))
    monkeypatch.setenv('LOGISTIK_SITE_ADMIN_SETUP_UNTIL',(expiry-timedelta(days=2)).isoformat())
    assert operator.post(root+'login',json={'login':'admin','password':'another-initial-test'}).status_code==401
