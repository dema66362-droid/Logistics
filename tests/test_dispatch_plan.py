"""Delivery, schedules, confirmed times and multi-company rosters through the public API."""
from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy import select

import app as core
from test_participants_28 import accounts, invite, post, respond, state, token, PASSWORD
from test_release_29 import new_order, relogin

def link(ship, carrier, email):
    invitation=invite(ship,email,'partner','carrier')
    assert respond(carrier,invitation).status_code==200

def vehicle(client):
    r=post(client,'/api/carrier/vehicles',{'label':'Учебный тягач №1','kind':'Фура','capacity':24})
    assert r.status_code==201
    return state(client)['vehicles'][0]['id']

def publish(ship,ref,carrier):
    return post(ship,f'/api/orders/{ref}/search',{'carrier_ids':[state(carrier)['user']['org_id']]})

def plan(client,week):
    r=client.get('/api/plan',params={'week':week})
    assert r.status_code==200,r.text
    return r.json()

def create_bid(carrier,ref,driver_id,vid,day,end=None,delivery=None,time='11:15',terms_version=1):
    return post(carrier,'/api/offers',dict(order_ref=ref,driver_id=driver_id,vehicle_id=vid,
        day=day,pickup=time,end_date=end or day,delivery_day=delivery or day,price=90000,accept_terms=True,terms_version=terms_version))

def members(carrier,name,email=''):
    r=post(carrier,'/api/carrier/roster',dict(name=name,email=email))
    assert r.status_code in (200,201),r.text
    return r.json()

def test_addressed_inbox_without_fleet_and_no_broadcast(accounts):
    ship,car,other=accounts('shipper'),accounts('carrier'),accounts('carrier','second@example.test')
    link(ship,car,'carrier@example.test')
    link(ship,other,'second@example.test')
    vehicle(other)
    ref,day=new_order(ship)
    assert plan(car,day)['inbox']==[]
    assert post(ship,f'/api/orders/{ref}/search',{'carrier_ids':[]}).status_code==422
    delivered=publish(ship,ref,car)
    assert delivered.status_code==200 and delivered.json()['notified']==1
    assert plan(car,day)['inbox']==[ref] and plan(other,day)['inbox']==[]
    assert sum(n['orderId']==ref for n in state(car)['notifications'])==1
    assert publish(ship,ref,car).status_code==409
    assert sum(n['orderId']==ref for n in state(car)['notifications'])==1
    assert sum(n['orderId']==ref for n in state(other)['notifications'])==0
    assert post(ship,f'/api/orders/{ref}/cancel').status_code==200
    assert plan(car,day)['inbox']==[]
    assert sum(n['orderId']==ref for n in state(car)['notifications'])==2

def test_schedules_each_stage_alternative_dates_and_fields(accounts):
    ship,car,driver=accounts('shipper'),accounts('carrier'),accounts('driver')
    link(ship,car,'carrier@example.test')
    d=members(car,'Водитель для подачи','driver@example.test')
    assert d['canReceive']
    vid=vehicle(car)
    day=core.business_today()+timedelta(days=8)
    monday=day+timedelta(days=(7-day.weekday())%7)
    scheduled=monday+timedelta(days=1)
    delivery=scheduled+timedelta(days=1)
    end=delivery+timedelta(days=2)
    ref=post(ship,'/api/orders',dict(from_city='Город А',to_city='Город Б',recipient='Склад клиента',
        product='Пустая тара',tons=20,date=str(monday),time='09:00',unload=str(delivery),vehicle_type='Фура',
        mode='Доставка',budget=100000,terms='Подача ко вторым воротам',pickup_company='Сторонний отправитель',
        pickup_address='Учебная 1',delivery_address='Учебная 2',recipient_contact='Контакт получателя',
        pickup_contact='Контакт отправителя')).json()['id']
    assert publish(ship,ref,car).status_code==200
    q=create_bid(car,ref,d['id'],vid,str(scheduled),str(end),str(delivery)).json()['id']
    ix=q.split('-')[1]
    for user in (car,driver):
        row=plan(user,str(monday))['drivers'][0]['jobs'][0]
        assert row['offer']['status']=='driver_ok' and not row['confirmed'] and row['offer']['day']==str(monday) and row['offer']['bidDay']==str(scheduled)
    assert not state(driver)['orders'][0]['pickupContact']
    assert post(driver,f'/api/offers/{ix}/driver-response?accepted=true').status_code==200
    assert plan(car,str(monday))['drivers'][0]['jobs'][0]['offer']['status']=='driver_ok'
    proposal=state(ship)['orders'][0]['slotProposals'][0]
    assert post(ship,f'/api/offers/{ix}/accept').status_code==409
    assert post(ship,f'/api/orders/{ref}/slot/'+str(proposal['id']),{'accepted':True}).status_code==200
    assert post(ship,f'/api/offers/{ix}/accept').status_code==200
    for user in (ship,car,driver):
        order=state(user)['orders'][0]
        assert (order['date'],order['time'],order['unload'])==(str(scheduled),'11:15',str(delivery))
        assert (order['requestedDate'],order['requestedTime'])==(str(monday),'09:00')
        assert order['pickupCompany']=='Сторонний отправитель' and order['pickupContact']=='Контакт отправителя'
        assert order['recipient']=='Склад клиента' and order['deliveryAddress']=='Учебная 2'
    assert state(driver)['orders'][0]['price']==0
    schedule=plan(ship,str(monday))
    assert schedule['queue'][0]['orders']==[] and schedule['queue'][1]['vehicleCount']==1
    assert schedule['queue'][1]['orders'][0]['id']==ref
    carrier_job=plan(car,str(monday))['drivers'][0]['jobs'][0]
    assert carrier_job['offer']['endDate']==str(end) and carrier_job['confirmed']
    assert post(ship,f'/api/orders/{ref}/complete').status_code==200
    assert state(driver)['orders'][0]['status']=='completed'

def test_unregistered_driver_can_be_planned_and_later_signs_in_by_personal_invitation(accounts):
    ship,car=accounts('shipper'),accounts('carrier')
    link(ship,car,'carrier@example.test')
    member=members(car,'Будущий водитель','newdriver@example.test')
    assert not member['canReceive'] and members(car,'Будущий водитель','newdriver@example.test')['id']==member['id']
    ref,day=new_order(ship);vid=vehicle(car)
    assert publish(ship,ref,car).status_code==200
    q=create_bid(car,ref,member['id'],vid,day).json()['id']
    assert not plan(car,day)['drivers'][0]['jobs'][0]['canReceive']
    inv=invite(car,'newdriver@example.test','team','driver')
    driver=accounts('driver','newdriver@example.test',invitation_token=token(inv))
    assert state(driver)['drivers'][0]['id']==member['id']
    assert state(driver)['offers'][0]['id']==q
    assert any(n['kind']=='crew_reserved' and n['orderId']==ref for n in state(driver)['notifications'])
    assert post(driver,'/api/offers/'+q[2:]+'/driver-response?accepted=true').status_code==200

def test_two_carriers_one_person_conflict_decline_and_release(accounts):
    ship=accounts('shipper');first=accounts('carrier');second=accounts('carrier','two@example.test');driver=accounts('driver')
    link(ship,first,'carrier@example.test')
    link(ship,second,'two@example.test')
    v1,v2=vehicle(first),vehicle(second)
    d1=members(first,'Сменный водитель','driver@example.test')
    d2=members(second,'Сменный водитель','driver@example.test')
    assert d1['id']==d2['id']
    assert len(driver.get('/api/participants').json()['companies'])==2
    ref,day=new_order(ship);ref2,_=new_order(ship)
    assert publish(ship,ref,first).status_code==200 and publish(ship,ref2,second).status_code==200
    qa=create_bid(first,ref,d1['id'],v1,day).json()['id'][2:]
    assert create_bid(second,ref2,d2['id'],v2,day).status_code==409
    assert len(plan(driver,day)['drivers'][0]['jobs'])==1
    assert [j['order']['id'] for j in plan(first,day)['drivers'][0]['jobs']]==[ref]
    assert post(first,f'/api/offers/{qa}/withdraw').status_code==200
    qb=create_bid(second,ref2,d2['id'],v2,day).json()['id'][2:]
    assert plan(driver,day)['drivers'][0]['jobs'][0]['order']['id']==ref2
    assert post(second,f'/api/offers/{qb}/withdraw').status_code==200
    assert plan(second,day)['drivers'][0]['jobs']==[] and plan(second,day)['inbox']==[ref2]
    assert post(second,f"/api/carrier/drivers/{d2['id']}/unlink").status_code==200
    assert not state(second)['drivers'] and state(first)['drivers'][0]['id']==d1['id']
    assert len(driver.get('/api/participants').json()['companies'])==1

def test_month_boundary_and_private_local_roster_removal(accounts):
    car=accounts('carrier')
    d=members(car,'Водитель без почты')
    assert not d['canReceive']
    assert plan(car,'2026-12-01')['dates']==['2026-11-30','2026-12-01','2026-12-02','2026-12-03','2026-12-04','2026-12-05','2026-12-06']
    assert post(car,f"/api/carrier/drivers/{d['id']}/unlink").status_code==200
    assert not state(car)['drivers']
    assert members(car,'Водитель без почты')['id']==d['id']
    assert len(state(car)['drivers'])==1

def test_addressed_carrier_declines_once_and_shipper_sees_receipt(accounts):
    ship,car=accounts('shipper'),accounts('carrier')
    link(ship,car,'carrier@example.test')
    ref,day=new_order(ship)
    assert publish(ship,ref,car).status_code==200
    assert state(ship)['orders'][0]['recipients'][0]['status']=='delivered'
    assert post(car,f'/api/orders/{ref}/decline-incoming').status_code==200
    assert post(car,f'/api/orders/{ref}/decline-incoming').status_code==200
    assert state(ship)['orders'][0]['recipients'][0]['status']=='declined'
    assert len([n for n in state(ship)['notifications'] if n['kind']=='carrier_declined'])==1
    assert plan(car,day)['inbox']==[]

def test_cross_company_busy_days_only_after_driver_partnership(accounts):
    ship=accounts('shipper');first=accounts('carrier');second=accounts('carrier','two@example.test');driver=accounts('driver')
    link(ship,first,'carrier@example.test')
    assert respond(first,invite(driver,'carrier@example.test','driver_join','carrier')).status_code==200
    relogin(driver,'driver@example.test')
    member=members(second,'Водитель','driver@example.test')
    ref,day=new_order(ship);vid=vehicle(first)
    assert publish(ship,ref,first).status_code==200
    q=create_bid(first,ref,member['id'],vid,day).json()['id'][2:]
    assert post(driver,f'/api/offers/{q}/driver-response?accepted=true').status_code==200
    before=plan(second,day)['drivers'][0]
    assert not before['externalAvailabilityKnown'] and before['busyOutside']==[]
    inv=invite(driver,'two@example.test','driver_join','carrier')
    assert respond(second,inv).status_code==200
    masked=plan(second,day)['drivers'][0]
    assert masked['busyOutside']==[day] and masked['jobs']==[] and ref not in str(masked)
    assert post(driver,"/api/participants/carriers/"+str(state(second)['user']['org_id'])+"/leave").status_code==200
    assert not state(second)['drivers']

def test_non_dispatchable_cargo_is_delivered_for_review_only(accounts):
    ship=accounts('shipper');car=accounts('carrier');other=accounts('carrier','outside@example.test');driver=accounts('driver')
    link(ship,car,'carrier@example.test');link(ship,other,'outside@example.test')
    cid=state(car)['user']['org_id'];day=str(core.business_today()+timedelta(days=5))
    ref=post(ship,'/api/orders',dict(from_city='Город А',to_city='Город Б',recipient='Склад',
        product='Спирт изопропиловый',tons=20,date=day,time='08:45',unload=day,vehicle_type='Фура',mode='Доставка',terms='Предварительная проверка')).json()['id']
    assert publish(ship,ref,car).status_code==409
    for replay in [False,True]:
        res=post(ship,f'/api/orders/{ref}/request-review',{'carrier_ids':[cid]})
        assert res.status_code==200 and res.json()['replayed']==replay
    assert plan(car,day)['reviewInbox']==[ref] and plan(car,day)['inbox']==[]
    assert not state(other)['orders'] and not state(driver)['orders']
    assert post(car,f'/api/orders/{ref}/acknowledge-review').json()['movementAuthorized'] is False
    assert state(ship)['orders'][0]['recipients'][0]['status']=='review_seen'
    assert state(car)['orders'][0]['searchLaunched'] is False
    v=vehicle(car);d=members(car,'Водитель','driver@example.test')
    assert create_bid(car,ref,d['id'],v,day).status_code==409
    assert not plan(driver,day)['drivers'][0]['jobs']

def internal_payload(did,vid,day,key='manual-future-trip-one'):
    return dict(request_id=key,customer='Клиент по телефону',driver_id=did,vehicle_id=vid,shipment=dict(
        from_city='Учебный город А',to_city='Учебный город Б',recipient='Склад клиента',product='Пустая тара',tons=20,
        date=day,time='12:40',unload=day,vehicle_type='Фура',mode='Доставка',terms='Учебная запись',
        pickup_company='Склад отправителя',pickup_address='Адрес А',delivery_address='Адрес Б',
        pickup_contact='Диспетчер А',recipient_contact='Диспетчер Б'))

def test_carrier_enters_external_work_once_and_driver_can_complete_cycle(accounts):
    ship=accounts('shipper');car=accounts('carrier');driver=accounts('driver');other=accounts('carrier','other@example.test')
    person=members(car,'Водитель','driver@example.test');vid=vehicle(car)
    day=str(core.business_today()+timedelta(days=14))
    payload=internal_payload(person['id'],vid,day)
    res=post(car,'/api/carrier/plan/trips',payload);assert res.status_code==201,res.text
    ref=res.json()['id']
    assert post(car,'/api/carrier/plan/trips',payload).json()=={'id':ref,'replayed':True}
    assert post(car,'/api/carrier/plan/trips',{**payload,'customer':'Другая компания'}).status_code==409
    assert not state(ship)['orders'] and not state(other)['orders']
    for client in (car,driver):
        job=plan(client,day)['drivers'][0]['jobs'][0]
        assert job['order']['shipperName']=='Клиент по телефону' and job['offer']['pickup']=='12:40' and job['confirmed']
    assert plan(car,day)['inbox']==[]
    assert state(driver)['orders'][0]['pickupContact']=='Диспетчер А'
    assert state(driver)['orders'][0]['pickupContact']=='Диспетчер А'
    assert plan(car,day)['drivers'][0]['jobs'][0]['confirmed']
    assert post(car,f'/api/carrier/plan/trips/{ref}/complete').status_code==200
    assert state(driver)['orders'][0]['status']=='completed'
    for client in (car,driver):
        assert len([n for n in state(client)['notifications'] if n['orderId']==ref])==2

def test_private_trip_blocks_normal_order_and_cancellation_releases_resources(accounts):
    ship=accounts('shipper');car=accounts('carrier');driver=accounts('driver');link(ship,car,'carrier@example.test')
    d=members(car,'Водитель','driver@example.test');vid=vehicle(car)
    ref,day=new_order(ship);assert publish(ship,ref,car).status_code==200
    private=post(car,'/api/carrier/plan/trips',internal_payload(d['id'],vid,day)).json()['id']
    assert create_bid(car,ref,d['id'],vid,day).status_code==409
    assert post(car,f'/api/carrier/plan/trips/{private}/cancel').status_code==200
    assert create_bid(car,ref,d['id'],vid,day).status_code==201
    assert post(car,'/api/carrier/plan/trips',internal_payload(d['id'],vid,day,'different-manual-key')).status_code==409

def test_internal_schedule_does_not_release_unverified_cargo(accounts):
    car=accounts('carrier');driver=accounts('driver')
    d=members(car,'Водитель','driver@example.test');vid=vehicle(car);day=str(core.business_today()+timedelta(days=6))
    p=internal_payload(d['id'],vid,day)
    p['shipment']['product']='Спирт изопропиловый'
    assert post(car,'/api/carrier/plan/trips',p).status_code==409
    assert not state(car)['orders'] and not state(driver)['orders']

def test_dispatcher_can_link_a_nameless_email_field_after_planning(accounts):
    ship=accounts('shipper');car=accounts('carrier');driver=accounts('driver');link(ship,car,'carrier@example.test')
    placeholder=members(car,'Иванов Пётр');vid=vehicle(car);ref,day=new_order(ship);assert publish(ship,ref,car).status_code==200
    q=create_bid(car,ref,placeholder['id'],vid,day).json()['id']
    assert not state(driver)['offers']
    connected=post(car,f"/api/carrier/roster/{placeholder['id']}/contact",{'email':'driver@example.test'})
    assert connected.status_code==200 and connected.json()['canReceive']
    assert len(state(car)['drivers'])==1 and state(driver)['offers'][0]['id']==q
    assert post(driver,f'/api/offers/{q[2:]}/driver-response?accepted=true').status_code==200
