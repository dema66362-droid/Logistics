from datetime import date, datetime, timedelta
from sqlalchemy import select
import app as c
from test_participants_28 import accounts, post, state
from test_monthly_dispatch import setup_trip
from test_dispatch_plan import create_bid, link, members, vehicle


def put(client,path,data):
    return client.put(path,json=data,headers={'X-CSRF-Token':state(client)['csrf']})


def test_route_cascade_permissions_and_reported_position(accounts):
    ship,car,driver,ref,day,d,v=setup_trip(accounts)
    with c.db_write() as db:
        trip=db.scalar(select(c.Shipment).where(c.Shipment.ref==ref))
        trip.from_city='Орск';trip.to_city='Москва'
    q=create_bid(car,ref,d,v,day,time='09:00').json()['id']
    assert post(ship,'/api/offers/'+q[2:]+'/accept').is_success
    url='/api/driver/routes/'+q
    def route(client):
        return client.get('/api/plan',params={'week':day}).json()['drivers'][0]['jobs'][0]['route']
    old=route(driver)
    assert old['stops'][0]['city']=='Орск' and old['stops'][-1]['city']=='Москва'
    assert len(old['stops'])>2 and old['totalKm']>1000
    eta=datetime.fromisoformat(old['stops'][1]['eta'])+timedelta(hours=8)
    body=dict(action='eta',version=old['version'],index=1,eta=eta.isoformat())
    assert put(car,url,body).status_code==403
    assert put(driver,url,body).is_success
    assert put(driver,url,body).status_code==409
    updated=route(car)
    for target,start in zip(updated['stops'][1:],old['stops'][1:]):
        assert datetime.fromisoformat(target['eta'])-datetime.fromisoformat(start['eta'])==timedelta(hours=8)
    for act in ['arrive','depart']:
        r=put(driver,url,dict(version=updated['version'],action=act,index=0))
        assert r.is_success,r.text
        updated=r.json()['route']
    assert [e['action'] for e in route(car)['events']]==['arrive','depart']
    bad=put(driver,url,dict(version=updated['version'],action='arrive',index=2))
    assert bad.status_code==409


def test_later_crew_offer_can_win_and_reason_survives_refresh(accounts):
    ship,car,driver,ref,day,d,v=setup_trip(accounts)
    car2=accounts('carrier','other@example.test')
    link(ship,car2,'other@example.test')
    d2=members(car2,'Другой Водитель')['id'];v2=vehicle(car2)
    with c.db_write() as db:
        order=db.scalar(select(c.Shipment).where(c.Shipment.ref==ref))
        db.add(c.DispatchRecipient(shipment_id=order.id,carrier_id=state(car2)['user']['org_id']))
    start=date.fromisoformat(day)
    crew=post(car,'/api/carrier/crew',dict(driver_id=d,legacy_vehicle=v,tractor='К123КК77',trailer='АВ123477',kind='Фура',capacity=24)).json()
    assert post(car,'/api/fleet/position',dict(vehicle_id=crew['vehicleId'],city='Орск',home='Орск',available=str(start+timedelta(days=1)))).is_success
    candidates=car.get('/api/fleet/candidates/'+ref).json()['items']
    assert candidates[0]['name'] and candidates[0]['delay']==1
    late=str(start+timedelta(days=1));later=str(start+timedelta(days=2))
    win=create_bid(car,ref,d,crew['vehicleId'],late,time='09:00').json()['id']
    assert post(car2,'/api/offers',dict(order_ref=ref,driver_id=d2,vehicle_id=v2,day=later,
        pickup='09:00',end_date=later,price=110000,accept_terms=True)).status_code==201
    assert post(ship,'/api/offers/'+win[2:]+'/accept?accept_proposed_date=true').is_success
    assert state(ship)['orders'][0]['date']==late
    notes=[n for n in state(car2)['notifications'] if n['kind']=='not_selected']
    assert len(notes)==1 and 'низкая цена' in notes[0]['message'] and 'ранняя подача' in notes[0]['message']
    assert '90000' not in notes[0]['message']
    n=notes[0]['id']
    assert post(car,'/api/notifications/read-visible',dict(ids=[n])).is_success
    assert not next(x for x in state(car2)['notifications'] if x['id']==n)['read']
    assert post(car2,'/api/notifications/read-visible',dict(ids=[n])).is_success
    assert next(x for x in state(car2)['notifications'] if x['id']==n)['read']


def test_leave_edit_keeps_id_and_rolls_back_overlap(accounts):
    ship,car,driver,ref,day,d,v=setup_trip(accounts)
    start=date.fromisoformat(day)+timedelta(days=9)
    create=lambda i:post(driver,'/api/driver/leave',dict(start=str(start+timedelta(days=i)),end=str(start+timedelta(days=i)))).json()
    first=create(0);second=create(4)
    url='/api/driver/leave/'+str(first['id'])
    body=dict(start=first['start'],end=second['end'],previous_start=first['start'],previous_end=first['end'])
    assert put(driver,url,body).status_code==409
    body['end']=str(start+timedelta(days=1))
    assert put(driver,url,body).json()['id']==first['id']
    assert driver.get('/api/driver/leave').json()[0]['end']==body['end']
