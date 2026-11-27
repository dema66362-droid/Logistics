from datetime import date, timedelta
from test_participants_28 import accounts, post, state
from test_monthly_dispatch import setup_trip
from test_dispatch_plan import publish, create_bid
from test_release_29 import new_order


def test_reschedule_version_and_participants(accounts):
    ship, car, driver, ref, initial, did, _ = setup_trip(accounts)
    crew = post(car, '/api/carrier/crew', dict(driver_id=did, tractor='А 123 АА 77',
                trailer='АВ-1234 77', kind='Фура', capacity=24))
    assert crew.status_code==200, crew.text
    assert state(driver)['crews'][0]['tractor']=='A123AA77'
    foreign=accounts('carrier','outsider@example.test')
    assert state(foreign)['crews']==[]
    assert post(foreign,'/api/carrier/crew',dict(driver_id=did,tractor='К123ММ77',
                trailer='ХК644177',kind='Фура',capacity=24)).status_code==403

    q=create_bid(car,ref,did,crew.json()['vehicleId'],initial,time='09:00').json()
    endpoint='/api/offers/'+q['id'][2:]
    assert q['status']=='driver_ok'
    assert post(ship,endpoint+'/accept').status_code==200
    later=str(date.fromisoformat(initial)+timedelta(days=9))
    for start,end,version in [(initial,later,1),(later,initial,2)]:
        assert post(ship,'/api/orders/'+ref+'/slot',dict(day=end,time='09:00',
                    expected_day=start,expected_time='09:00')).status_code==200
        assert all(state(p)['orders'][0]['unload']==end for p in (ship,car,driver))
        assert post(foreign,endpoint+'/confirm-date',{'version':version}).status_code==403
        if version==2:
            assert post(driver,endpoint+'/confirm-date',{'version':1}).status_code==409
        for party in (car,driver):
            assert post(party,endpoint+'/confirm-date',{'version':version}).status_code==200
        count=len(state(ship)['notifications'])
        assert post(car,endpoint+'/confirm-date',{'version':version}).status_code==200
        assert len(state(ship)['notifications'])==count
    assert post(ship,'/api/orders/'+ref+'/complete').status_code==200


def test_shared_trailer_cannot_be_reserved_twice(accounts):
    ship, car, _, ref, day, person, _=setup_trip(accounts)
    def crew(**extra):
        result=post(car,'/api/carrier/crew',dict(trailer='АО 1234 77',kind='Фура',capacity=24,**extra))
        assert result.is_success,result.text
        return result.json()
    first=crew(driver_id=person,tractor='А111АА77')
    other=crew(name='Второй Водитель',tractor='К222КК77')
    assert create_bid(car,ref,first['driverId'],first['vehicleId'],day).status_code==201
    another,_=new_order(ship)
    assert publish(ship,another,car).is_success
    assert create_bid(car,another,other['driverId'],other['vehicleId'],day).status_code==409
