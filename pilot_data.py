"""Deterministic demo fixtures for local and cloud pilot installations.

Creates ONLY fictitious non-hazardous data; never imports customer spreadsheets.
"""
from datetime import date, timedelta
from sqlalchemy import select
from app import (Organization, User, Driver, Vehicle, Approval, Shipment,
                 db_write, SessionLocal, pass_hash)


def create_pilot_data(passwords: str | dict[str, str]) -> bool:
    """Create five synthetic users and one draft; return False if any users exist.

    String is accepted for legacy *local* test fixtures only.
    Cloud deployment supplies five distinct passwords by email.
    """
    with SessionLocal() as db:
        if db.scalar(select(User.id).limit(1)) is not None:
            return False
    with db_write() as db:
        if db.scalar(select(User.id).limit(1)) is not None:
            return False
        shipper = Organization(name='Тест: заказчик А', kind='shipper')
        carrier_b = Organization(name='Тест: перевозчик Б', kind='carrier')
        carrier_c = Organization(name='Тест: перевозчик В', kind='carrier')
        db.add_all([shipper, carrier_b, carrier_c]); db.flush()
        accounts = [
            ('pilot.shipper@example.test', 'Логист', 'shipper', shipper),
            ('pilot.carrier1@example.test', 'Диспетчер Б', 'carrier', carrier_b),
            ('pilot.driver1@example.test', 'Водитель Б', 'driver', carrier_b),
            ('pilot.carrier2@example.test', 'Диспетчер В', 'carrier', carrier_c),
            ('pilot.driver2@example.test', 'Водитель В', 'driver', carrier_c),
        ]
        for email, label, role, org in accounts:
            pw = passwords[email] if isinstance(passwords, dict) else passwords
            u=User(org_id=org.id, email=email, name=label, role=role, is_admin=role!='driver', password_hash=pass_hash(pw))
            db.add(u); db.flush()
            if role=='driver': db.add(Driver(org_id=org.id, user_id=u.id, name=label))
        db.add_all([
            Approval(shipper_id=shipper.id, carrier_id=carrier_b.id, active=True),
            Approval(shipper_id=shipper.id, carrier_id=carrier_c.id, active=True),
            Vehicle(org_id=carrier_b.id,label='Тестовая фура Б-01', kind='Фура',capacity=25),
            Vehicle(org_id=carrier_c.id,label='Тестовая фура В-01', kind='Фура',capacity=25),
        ])
        start=date.today()+timedelta(days=2)
        db.add(Shipment(ref='TEST-1001',shipper_id=shipper.id,
            from_city='Тестовый город А',to_city='Тестовый город Б',
            recipient='Учебный склад получателя',product='Вода техническая',tons=20,
            pickup_date=start,pickup_time='10:00',unload_date=start+timedelta(days=1),
            vehicle_kind='Фура',mode='Доставка',budget=90000,rate=4500,
            manager='Тестовый логист',hazardous=False,status='search',
            pickup_address='Условная улица Погрузки, 1',
            delivery_address='Условная улица Разгрузки, 2',
            recipient_contact='Тестовый диспетчер — без телефона',
            terms='Учебный рейс без опасных грузов. Водитель должен подтвердить свободный график.',
            search_launched=False,public_external=False,tariff_source='manual',
            note='Демонстрационный заказ, без реальной доставки'))
    print('Pilot fixtures ready: 5 synthetic logins, 1 draft order. No passwords logged.')
    return True
