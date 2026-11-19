"""Three independent role sessions exercise loading slots, holidays and account cards."""
from datetime import date,timedelta
from calendar import monthrange

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app as core
from test_participants_28 import accounts, post, state, PASSWORD
from test_dispatch_plan import members, vehicle, publish, link, create_bid
from test_release_29 import new_order


def setup_trip(accounts):
    ship,car,driver=accounts('shipper'),accounts('carrier'),accounts('driver')
    link(ship,car,'carrier@example.test')
    member=members(car,'Тестовый водитель','driver@example.test')
    ref,day=new_order(ship)
    assert publish(ship,ref,car).status_code==200
    return ship,car,driver,ref,day,member['id'],vehicle(car)


def slot(day,time,current='09:00'):
    return dict(day=day,time=time,expected_day=day,expected_time=current)


def test_default_slot_counteroffer_decision_and_idempotency(accounts):
    ship,car,driver,ref,day,d,v=setup_trip(accounts)
    outsider=accounts('carrier','other@example.test')
    url='/api/orders/'+ref+'/slot'
    assert state(car)['orders'][0]['time']=='09:00'
    assert post(outsider,url,slot(day,'08:30')).status_code==403
    p=post(car,url,slot(day,'08:30')).json()
    assert p['changed'] and not post(car,url,slot(day,'08:30')).json()['changed']
    # No pricing or crew decision can put an unapproved earlier time into the queue.
    body=dict(order_ref=ref,driver_id=d,vehicle_id=v,day=day,pickup='08:30',end_date=day,price=100000,accept_terms=True)
    bid=post(car,'/api/offers',body).json()['id'][2:]
    for who in (ship,car,driver):
        o=state(who)['orders'][0]
        assert (o['date'],o['time'])==(day,'09:00')
        assert len(o['slotProposals'])==1 and o['slotProposals'][0]['time']=='08:30'
    assert post(driver,f'/api/offers/{bid}/driver-response?accepted=true').status_code==200
    assert not state(ship)['orders'][0]['selectedOffer']
    assert post(ship,f'/api/offers/{bid}/accept').status_code==409
    assert post(driver,url+'/'+str(p['id']),{'accepted':True}).status_code==403
    decision=url+'/'+str(p['id'])
    assert post(ship,decision,{'accepted':True}).status_code==200
    before=len(state(ship)['notifications'])
    assert post(ship,decision,{'accepted':True}).status_code==200
    assert len(state(ship)['notifications'])==before
    assert post(ship,f'/api/offers/{bid}/accept').status_code==200
    for who in (ship,car,driver):
        o=state(who)['orders'][0]
        assert (o['requestedTime'],o['time'],o['status'],o['slotProposals'])==('09:00','08:30','confirmed',[])
    p=post(driver,url,slot(day,'10:30','08:30')).json()
    assert state(ship)['orders'][0]['slotProposals'][0]['role']=='driver'
    assert post(ship,url+'/'+str(p['id']),{'accepted':False}).status_code==200
    assert state(driver)['orders'][0]['time']=='08:30'
    assert post(ship,url,slot(day,'12:10','08:30')).status_code==200
    assert post(driver,url,slot(day,'12:15','08:30')).status_code==409
    assert all(state(c)['orders'][0]['time']=='12:10' for c in (ship,car,driver))
    assert {n['kind'] for n in state(driver)['notifications']} >= {'slot_changed','slot_proposed','slot_rejected'}


def test_leave_across_months_conflicts_and_isolation(accounts):
    ship,car,driver,ref,day,d,v=setup_trip(accounts)
    other=accounts('driver','different@example.test')
    begin=date.fromisoformat(day);first=begin.replace(day=1)
    next_month=first+timedelta(days=monthrange(first.year,first.month)[1])
    start=next_month-timedelta(days=1);end=next_month+timedelta(days=2)
    if start<begin:start=begin
    period=dict(start=str(start),end=str(end))
    vacation=post(driver,'/api/driver/leave',period)
    assert vacation.status_code==201
    assert post(driver,'/api/driver/leave',period).status_code==200
    assert post(car,'/api/driver/leave',period).status_code==403
    assert post(driver,'/api/driver/leave',dict(start=str(start),end=str(end+timedelta(days=1)))).status_code==409
    for day_in_month in (start,end):
        path='/api/plan/month?month='+day_in_month.strftime('%Y-%m')
        result=driver.get(path).json()
        assert len(result['dates'])==monthrange(day_in_month.year,day_in_month.month)[1]
        assert result['drivers'][0]['leave']==[vacation.json()]
        assert other.get(path).json()['drivers'][0]['leave']==[]
        assert car.get(path).status_code==403
    assert create_bid(car,ref,d,v,str(start),time='09:00').status_code==409
    assert post(other,'/api/driver/leave/'+str(vacation.json()['id'])+'/remove').status_code==200
    assert len(driver.get(path).json()['drivers'][0]['leave'])==1
    assert post(driver,'/api/driver/leave/'+str(vacation.json()['id'])+'/remove').status_code==200
    bid=create_bid(car,ref,d,v,day,time='09:00').json()['id'][2:]
    assert post(driver,f'/api/offers/{bid}/driver-response?accepted=true').status_code==200
    assert post(driver,'/api/driver/leave',dict(start=day,end=day)).status_code==409
    assert driver.get('/api/plan/month?month=2028-02').json()['dates'][-1]=='2028-02-29'
    assert driver.get('/api/plan/month?month=2027-02').json()['dates'][-1]=='2027-02-28'
    assert driver.get('/api/plan/month?month=0000-01').status_code==422


def test_inn_company_validation_inheritance_and_legacy(accounts):
    company=accounts('shipper');other=accounts('carrier');driver=accounts('driver',inn='')
    assert company.get('/api/account').json()['inn']=='0000000018'
    assert driver.get('/api/account').json()['inn'] is None
    assert post(driver,'/api/account/inn',{'inn':'0000000018'}).status_code==403
    guest=TestClient(core.app)
    for role in ('shipper','carrier'):
        body=dict(role=role,name='Тестовый участник',organization='Тестовая организация',
                  email=role+'.new@example.test',password=PASSWORD)
        for bad in ('','123','0000000000','0000000019','00000000a8'):
            assert guest.post('/api/signup',json={**body,'inn':bad}).status_code==422
    # Twelve digits with computed check digits; retains leading zeros for a synthetic test card.
    personal='000000000184'
    org_id=state(company)['user']['org_id']
    with core.db_write() as db:
        db.get(core.Organization,org_id).inn=None
    assert company.get('/api/account').json()['inn'] is None
    assert post(company,'/api/account/inn',{'inn':personal}).status_code==200
    assert company.get('/api/account').json()['inn']==personal
    assert other.get('/api/account').json()['inn']=='0000000018'
    from test_participants_28 import invite,token
    inv=invite(company,'employee@example.test','team','shipper')
    employee=accounts('shipper','employee@example.test',inn='',invitation_token=token(inv))
    assert employee.get('/api/account').json()['inn']==personal
    assert post(employee,'/api/account/inn',{'inn':'0000000018'}).status_code==403
    legacy=dict(organization='Тест перевозчик',email='old-api@example.test',name='Тест имени',password=PASSWORD,
                vehicle_kind='Фура',vehicle_label='Тестовый ТС',capacity_tons=24)
    assert guest.post('/api/signup/carrier',json=legacy).status_code==422
    assert guest.post('/api/signup/carrier',json={**legacy,'inn':'0000000018'}).status_code==201


def test_number_of_roster_cards_and_monthly_tenant_boundary(accounts):
    car=accounts('carrier');outsider=accounts('carrier','others@example.test');csrf=state(car)['csrf']
    for n in range(105):
        r=car.post('/api/carrier/roster',json={'name':f'Учебный водитель {n}'},headers={'X-CSRF-Token':csrf})
        assert r.status_code==201
    assert len(state(car)['drivers'])==105 and state(outsider)['drivers']==[]
    assert len(car.get('/api/participants').json()['roster'])==105


def test_only_one_shipper_decision_can_move_a_disputed_slot(accounts):
    ship,first,second=accounts('shipper'),accounts('carrier'),accounts('carrier','second@example.test')
    link(ship,first,'carrier@example.test');link(ship,second,'second@example.test')
    ref,day=new_order(ship)
    targets=[state(c)['user']['org_id'] for c in (first,second)]
    assert post(ship,'/api/orders/'+ref+'/search',{'carrier_ids':targets}).status_code==200
    url='/api/orders/'+ref+'/slot'
    p=post(first,url,slot(day,'08:10')).json()['id']
    q=post(second,url,slot(day,'11:00')).json()['id']
    assert post(ship,url+'/'+str(p),{'accepted':True}).status_code==200
    assert post(ship,url+'/'+str(q),{'accepted':True}).status_code==409
    assert all(state(c)['orders'][0]['time']=='08:10' for c in (ship,first,second))
    # Returning to the existing official time withdraws only the sender's objection.
    assert post(first,url,slot(day,'10:05','08:10')).status_code==200
    assert post(first,url,slot(day,'08:10','08:10')).json()['changed']
    assert not state(ship)['orders'][0]['slotProposals']
