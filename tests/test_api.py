import os
import tempfile
from pathlib import Path

# Isolated database: never modify a real customer's information.
TEST_DIR=tempfile.TemporaryDirectory(prefix='logistik_tests_')
os.environ['LOGISTIK_DATABASE_URL']='sqlite:///'+str(Path(TEST_DIR.name)/'test.sqlite3')
os.environ['LOGISTIK_DEMO']='1'

from fastapi.testclient import TestClient
from app import app, SessionLocal, Shipment
from sqlalchemy import select

PASSWORD='Demo-2026-Start!'

def login(email):
    client=TestClient(app)
    r=client.post('/api/login',json={'email':email,'password':PASSWORD})
    assert r.status_code==200,r.text
    state=client.get('/api/state')
    assert state.status_code==200,state.text
    return client,state.json()['csrf']

def request(client,csrf,path,payload=None):
    return client.post(path,json=payload,headers={'X-CSRF-Token':csrf})

def create_order(client,csrf,product='Вода техническая',day='2026-12-02',to='Тестовая точка',recipient='Тестовый получатель'):
    p={'from_city':'Тестовый отправитель','to_city':to,'recipient':recipient,'product':product,
       'tons':24,'date':day,'time':'09:00','unload':day,'vehicle_type':'Автоцистерна',
       'mode':'Доставка','budget':100000,'terms':'Тестовые условия для обычного груза','pickup_address':'Условная площадка, 1','delivery_address':'Условная улица, д. 1','recipient_contact':'Тестовый склад: +7 000 000-00-00'}
    r=request(client,csrf,'/api/orders',p)
    if r.status_code==201 and r.json()['hazardous'] is False:
        published=request(client,csrf,'/api/orders/'+r.json()['id']+'/search',{'public_external':False})
        assert published.status_code==200,published.text
    return r

def create_offer(client,csrf,ref,day='2026-12-02',driver_id=1,vehicle_id=1,price=90000):
    return request(client,csrf,'/api/offers',{'order_ref':ref,'driver_id':driver_id,'vehicle_id':vehicle_id,
             'day':day,'pickup':'09:00','end_date':day,'price':price,'accept_terms':True})

def test_auth_permissions_and_csrf():
    guest=TestClient(app)
    assert guest.get('/api/state').status_code==401
    assert guest.post('/api/login',json={'email':'shipper@example.test','password':'incorrect'}).status_code==401
    ship,csrf=login('shipper@example.test')
    assert ship.post('/api/orders',json={'a':1}).status_code==422
    assert create_order(ship,'wrong-token').status_code==403
    wrong=request(ship,'no', '/api/offers/999/accept')
    assert wrong.status_code==403
    drv,dcsrf=login('driver@example.test')
    assert create_order(drv,dcsrf).status_code==403
    assert drv.get('/api/export.csv').status_code==403
    assert drv.get('/api/state').json()['offers']==[]


def test_hazardous_blocked_and_order_permissions():
    ship,sc=login('shipper@example.test')
    haz=create_order(ship,sc,'Спирт изопропиловый',day='2026-12-09')
    assert haz.status_code==201,haz.text
    assert haz.json()['hazardous'] is True
    carrier,cc=login('carrier@example.test')
    r=create_offer(carrier,cc,haz.json()['id'],day='2026-12-09')
    assert r.status_code==409,r.text
    assert 'опасного' in r.json()['detail'].lower()
    other,oc=login('carrier2@example.test')
    r=create_offer(other,oc,haz.json()['id'],day='2026-12-09',driver_id=2,vehicle_id=2)
    assert r.status_code==409
    assert create_offer(carrier,cc,'NOT-FOUND').status_code==404


def test_full_cycle_double_accept_and_audit():
    ship,sc=login('shipper@example.test')
    carrier,cc=login('carrier@example.test')
    drv,dc=login('driver@example.test')
    o=create_order(ship,sc,day='2026-12-12',to='Уфа')
    assert o.status_code==201,o.text
    ref=o.json()['id']
    q=create_offer(carrier,cc,ref,day='2026-12-12')
    assert q.status_code==201,q.text
    qid=int(q.json()['id'].split('-')[1])
    assert request(carrier,cc,f'/api/offers/{qid}/driver-response?accepted=true').status_code==403
    assert any(o['status']=='driver_ok' for o in drv.get('/api/state').json()['offers'])
    accepted=request(ship,sc,f'/api/offers/{qid}/accept')
    assert accepted.status_code==200,accepted.text
    assert accepted.json()['status']=='confirmed'
    assert accepted.json()['price']==90000
    assert request(ship,sc,f'/api/offers/{qid}/accept').status_code==409
    history=ship.get('/api/audit/'+ref).json()
    assert [x['action'] for x in history]==['created','search_launched','offer_created','assigned']
    assert ship.get('/api/export.csv').status_code==200
    # carrier sees only their own commercial offer, not a competitor's.
    assert all(x['carrier']=='Демо: Каскад Транс' for x in carrier.get('/api/state').json()['offers'])



def test_vehicle_conflict_and_isolation():
    ship,sc=login('shipper@example.test')
    carrier,cc=login('carrier@example.test')
    # same confirmed vehicle/driver on a second shipment must be rejected.
    o=create_order(ship,sc,day='2026-12-12',to='Челябинск')
    assert o.status_code==201
    r=create_offer(carrier,cc,o.json()['id'],day='2026-12-12')
    assert r.status_code==409,r.text
    o2=create_order(ship,sc,day='2026-12-15')
    assert o2.status_code==201
    invalid_vehicle=create_offer(carrier,cc,o2.json()['id'],day='2026-12-15',vehicle_id=2)
    assert invalid_vehicle.status_code==403
    invalid_driver=create_offer(carrier,cc,o2.json()['id'],day='2026-12-15',driver_id=2)
    assert invalid_driver.status_code==403
    valid=create_offer(carrier,cc,o2.json()['id'],day='2026-12-15')
    assert valid.status_code==201,valid.text


def test_csv_injection_protection_and_persistence():
    ship,sc=login('shipper@example.test')
    r=create_order(ship,sc,day='2026-12-16',recipient='=HYPERLINK("https://x.test","x")')
    assert r.status_code==201
    csv_bytes=ship.get('/api/export.csv').content
    assert b'HYPERLINK' in csv_bytes
    assert b"'=HYPERLINK" in csv_bytes
    second,sc2=login('shipper@example.test')
    assert r.json()['id'] in [o['id'] for o in second.get('/api/state').json()['orders']]


def test_input_validation_and_empty_response():
    ship,sc=login('shipper@example.test')
    fail=create_order(ship,sc,day='2026-12-19',product='')
    assert fail.status_code==422
    c,cc=login('carrier@example.test')
    assert create_offer(c,cc,'P-1048').status_code==409 # special cargo blocked


def test_two_carriers_compete_and_isolation():
    ship,sc=login('shipper@example.test')
    first,fc=login('carrier@example.test')
    second,scc=login('carrier2@example.test')
    driver1,dc1=login('driver@example.test')
    driver2,dc2=login('driver2@example.test')
    o=create_order(ship,sc,day='2026-12-23',to='Тестовый город')
    assert o.status_code==201
    ref=o.json()['id']
    one=create_offer(first,fc,ref,day='2026-12-23',price=110000)
    two=create_offer(second,scc,ref,day='2026-12-23',driver_id=2,vehicle_id=2,price=99000)
    assert one.status_code==201 and two.status_code==201,(one.text,two.text)
    q1=int(one.json()['id'][2:]);q2=int(two.json()['id'][2:])
    assert request(driver1,dc1,f'/api/offers/{q1}/driver-response?accepted=true').status_code==200
    assert request(driver2,dc2,f'/api/offers/{q2}/driver-response?accepted=true').status_code==200
    assert all(x['carrier'].endswith('Каскад Транс') for x in first.get('/api/state').json()['offers'])
    assert all(x['carrier'].endswith('Транзит-Урал') for x in second.get('/api/state').json()['offers'])
    all_offers=[q for q in ship.get('/api/state').json()['offers'] if q['orderId']==ref]
    assert len(all_offers)==2
    assert sorted(q['price'] for q in all_offers)==[99000,110000]
    res=request(ship,sc,f'/api/offers/{q2}/accept')
    assert res.status_code==200,res.text
    assert res.json()['carrier'].endswith('Транзит-Урал')
    assert request(ship,sc,f'/api/offers/{q1}/accept').status_code==409
    q1_after=next(q for q in ship.get('/api/state').json()['offers'] if q['id']==f'Q-{q1}')
    assert q1_after['status']=='not_selected'
    state=driver2.get('/api/state').json()
    assert all(not order['manager'] and order['budget']==0 for order in state['orders'])


def test_unknown_cargo_defaults_to_safety_review():
    ship,sc=login('shipper@example.test')
    resp=create_order(ship,sc,product='Неизвестный наливной продукт',day='2026-12-24')
    assert resp.status_code==201
    assert resp.json()['hazardous'] is True  # flag means 'blocked pending review' in pilot
    carrier,cc=login('carrier@example.test')
    assert create_offer(carrier,cc,resp.json()['id'],day='2026-12-24').status_code==409


def test_dispatchable_delivery_requires_essential_information():
    ship, sc = login('shipper@example.test')
    basic={'from_city':'Тестоград','to_city':'Зеленоград','recipient':'Демо склад',
           'product':'Вода техническая','tons':10,'date':'2027-10-10','time':'09:30',
           'unload':'2027-10-11','vehicle_type':'Автоцистерна','mode':'Доставка',
           'budget':75000,'terms':'Тестовые условия'}
    missing=request(ship,sc,'/api/orders',basic)
    assert missing.status_code==422
    assert 'погрузки' in missing.json()['detail'] and 'контакт' in missing.json()['detail']
    created=request(ship,sc,'/api/orders',{
        **basic,'pickup_address':'Тестовая, 1','delivery_address':'Условная, 2',
        'recipient_contact':'Диспетчер склада: 0000'})
    assert created.status_code==201,created.text
    assert created.json()['pickupAddress']=='Тестовая, 1'


def test_carrier_and_driver_get_full_addresses_only_after_assignment():
    ship,sc=login('shipper@example.test')
    carrier,cc=login('carrier2@example.test')
    driver,dc=login('driver2@example.test')
    day='2027-10-20'
    r=request(ship,sc,'/api/orders',{'from_city':'Город X','to_city':'Город Y',
         'pickup_address':'Тестовая площадка 14','delivery_address':'Тестовая улица 71',
         'recipient_contact':'Тестовый склад 555','recipient':'Тестовый получатель',
         'product':'Вода техническая','tons':12,'date':day,'time':'10:00',
         'unload':day,'vehicle_type':'Автоцистерна','mode':'Доставка',
         'budget':80000,'terms':'Условия тестирования'})
    assert r.status_code==201,r.text
    ref=r.json()['id']
    assert request(ship,sc,f'/api/orders/{ref}/search',{'public_external':False}).status_code==200
    bid=request(carrier,cc,'/api/offers',{'order_ref':ref,'driver_id':2,'vehicle_id':2,
        'day':day,'pickup':'10:00','end_date':day,'price':82000,'accept_terms':True})
    assert bid.status_code==201,bid.text
    qid=int(bid.json()['id'][2:])
    hidden=next(x for x in driver.get('/api/state').json()['orders'] if x['id']==ref)
    assert hidden['pickupAddress']==hidden['deliveryAddress']==hidden['recipientContact']==''
    assert request(driver,dc,f'/api/offers/{qid}/driver-response?accepted=true').status_code==200
    assert request(ship,sc,f'/api/offers/{qid}/accept').status_code==200
    full_driver=next(x for x in driver.get('/api/state').json()['orders'] if x['id']==ref)
    full_carrier=next(x for x in carrier.get('/api/state').json()['orders'] if x['id']==ref)
    assert full_driver['pickupAddress']=='Тестовая площадка 14'
    assert full_carrier['deliveryAddress']=='Тестовая улица 71'
    assert full_driver['recipientContact']=='Тестовый склад 555'
