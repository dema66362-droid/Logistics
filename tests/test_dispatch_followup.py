"""Follow a real offer after the shipper edits the queue and after an unaddressed search."""
import csv
from io import StringIO

import pytest

from test_dispatch_plan import create_bid, link, members, plan, vehicle
from test_monthly_dispatch import setup_trip, slot
from test_participants_28 import accounts, post, state
from test_release_29 import new_order


def confirm_driver(driver, offer):
    assert offer.status_code == 201, offer.text
    number = offer.json()['id'][2:]
    response = post(driver, f'/api/offers/{number}/driver-response?accepted=true')
    assert response.status_code == 200, response.text
    return number


@pytest.mark.parametrize('chosen_time,compatible', [('10:00', False), ('08:30', True)])
def test_direct_shipper_decision_reconciles_the_actual_reservation(accounts, chosen_time, compatible):
    ship, car, driver, ref, day, person, truck = setup_trip(accounts)
    bid = confirm_driver(driver, create_bid(car, ref, person, truck, day, time='08:30'))
    assert state(ship)['orders'][0]['slotProposals']
    assert post(ship, f'/api/orders/{ref}/slot', slot(day, chosen_time)).status_code == 200
    order = state(ship)['orders'][0]
    assert order['time'] == chosen_time and order['slotProposals'] == []
    if compatible:
        assert post(ship, f'/api/offers/{bid}/accept').status_code == 200
        assert state(driver)['orders'][0]['time'] == chosen_time
    else:
        assert order['status'] == 'search'
        assert state(car)['offers'][0]['status'] == 'rejected'
        assert not plan(driver, day)['drivers'][0]['jobs']
        assert plan(car, day)['inbox'] == [ref]
        assert create_bid(car, ref, person, truck, day, time=chosen_time).status_code == 201


def test_accepting_a_competing_proposal_releases_only_the_incompatible_bid(accounts):
    ship, car, driver = accounts('shipper'), accounts('carrier'), accounts('driver')
    second = accounts('carrier', 'another@example.test')
    driver2 = accounts('driver', 'another.driver@example.test')
    link(ship, car, 'carrier@example.test')
    link(ship, second, 'another@example.test')
    ref, day = new_order(ship)
    ids = [state(c)['user']['org_id'] for c in (car, second)]
    assert post(ship, f'/api/orders/{ref}/search', {'carrier_ids': ids}).status_code == 200
    for company, person, email, time in [(car, driver, 'driver@example.test', '08:30'),
                                          (second, driver2, 'another.driver@example.test', '10:15')]:
        crew = members(company, 'Учебный водитель', email)
        confirm_driver(person, create_bid(company, ref, crew['id'], vehicle(company), day, time=time))
    proposal = next(p for p in state(ship)['orders'][0]['slotProposals'] if p['time'] == '10:15')
    assert post(ship, f'/api/orders/{ref}/slot/{proposal["id"]}', {'accepted': True}).status_code == 200
    assert state(car)['offers'][0]['status'] == 'rejected'
    assert not plan(driver, day)['drivers'][0]['jobs']
    remaining = state(second)['offers'][0]
    assert post(ship, '/api/offers/' + remaining['id'][2:] + '/accept').status_code == 200
    assert state(driver2)['orders'][0]['time'] == '10:15'


@pytest.mark.parametrize('public', [False, True])
def test_broadcast_recipient_gets_updates_before_bidding_once(accounts, public):
    ship, reached, outside = accounts('shipper'), accounts('carrier'), accounts('carrier', 'outside@example.test')
    if not public:
        link(ship, reached, 'carrier@example.test')
    vehicle(reached)
    ref, day = new_order(ship)
    sent = post(ship, f'/api/orders/{ref}/search', {'public_external': public})
    assert sent.status_code == 200 and sent.json()['notified'] == 1
    def kinds(client):
        return [n['kind'] for n in state(client)['notifications'] if n['orderId'] == ref]

    assert kinds(reached) == ['new_order']
    assert post(ship, f'/api/orders/{ref}/slot', slot(day, '10:25')).status_code == 200
    assert post(ship, f'/api/orders/{ref}/cancel').status_code == 200
    assert sorted(kinds(reached)) == ['new_order', 'order_cancelled', 'slot_changed']
    assert kinds(outside) == []


def test_private_search_updates_stop_when_pool_access_is_removed(accounts):
    ship, car = accounts('shipper'), accounts('carrier')
    link(ship, car, 'carrier@example.test')
    cid = state(car)['user']['org_id']
    vehicle(car)
    ref, day = new_order(ship)
    assert post(ship, f'/api/orders/{ref}/search', {'public_external': False}).json()['notified'] == 1
    assert post(ship, f'/api/shipper/carriers/{cid}/revoke').status_code == 200
    assert post(ship, f'/api/orders/{ref}/slot', slot(day, '10:20')).status_code == 200
    assert not any(n['kind'] == 'slot_changed' for n in state(car)['notifications'])


def test_csv_uses_the_same_current_loading_order_as_the_weekly_queue(accounts):
    ship = accounts('shipper')
    later, day = new_order(ship)
    early, _ = new_order(ship)
    assert post(ship, f'/api/orders/{later}/slot', slot(day, '14:15')).status_code == 200
    assert post(ship, f'/api/orders/{early}/slot', slot(day, '07:50')).status_code == 200
    group = next(g for g in plan(ship, day)['queue'] if g['date'] == day)
    expected = [(o['id'], o['time']) for o in group['orders']]
    assert expected == [(early, '07:50'), (later, '14:15')]
    download = ship.get('/api/export.csv')
    assert download.status_code == 200
    rows = csv.DictReader(StringIO(download.text.lstrip('\ufeff')), delimiter=';')
    assert [(r['Номер'], r['Время']) for r in rows] == expected


def test_external_carrier_can_propose_time_before_bidding_only_on_visible_orders(accounts):
    ship, car, other = accounts('shipper'), accounts('carrier'), accounts('carrier', 'no.truck@example.test')
    vehicle(car)
    ref, day = new_order(ship)
    url = f'/api/orders/{ref}/slot'
    assert post(car, url, slot(day, '10:10')).status_code == 403
    assert post(ship, f'/api/orders/{ref}/search', {'public_external': True}).json()['notified'] == 1
    assert post(other, url, slot(day, '10:10')).status_code == 403
    response = post(car, url, slot(day, '10:10'))
    assert response.status_code == 200, response.text
    assert state(ship)['orders'][0]['slotProposals'][0]['id'] == response.json()['id']
    assert post(ship, url + '/' + str(response.json()['id']), {'accepted': True}).status_code == 200
    order = state(car)['orders'][0]
    assert order['time'] == '10:10' and not order.get('pickupAddress')
    assert order['slotProposals'] == []
