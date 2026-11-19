"""Seed an EMPTY private pilot database with fictitious organizations and random passwords.
No real cargo, addresses, contacts or dangerous goods are created.
Never run on a database with existing users.
"""
import os
import secrets
from sqlalchemy import select
from app import Organization, User, Driver, Vehicle, Approval, db_write, pass_hash, DEMO

if DEMO or os.environ.get('LOGISTIK_ALLOW_SIGNUP') != '0':
    raise SystemExit('Refusing unsafe bootstrap: require LOGISTIK_DEMO=0 and LOGISTIK_ALLOW_SIGNUP=0')
if os.environ.get('LOGISTIK_SECURE_COOKIE') != '1':
    raise SystemExit('For HTTPS pilot: LOGISTIK_SECURE_COOKIE must be 1')

accounts = []
with db_write() as db:
    if db.scalar(select(User.id).limit(1)) is not None:
        raise SystemExit('Database already has accounts; no changes made')
    shipper = Organization(name='Пилот: заказчик А', kind='shipper')
    c1 = Organization(name='Пилот: перевозчик Б', kind='carrier')
    c2 = Organization(name='Пилот: перевозчик В', kind='carrier')
    db.add_all([shipper,c1,c2]); db.flush()
    for address, label, role, org in (
        ('pilot.shipper@example.test','Логист','shipper',shipper),
        ('pilot.carrier1@example.test','Диспетчер Б','carrier',c1),
        ('pilot.driver1@example.test','Водитель Б','driver',c1),
        ('pilot.carrier2@example.test','Диспетчер В','carrier',c2),
        ('pilot.driver2@example.test','Водитель В','driver',c2),
    ):
        password=secrets.token_urlsafe(18)
        user=User(org_id=org.id,email=address,name=label,role=role,password_hash=pass_hash(password))
        db.add(user);db.flush()
        accounts.append((label,address,password))
        if role=='driver': db.add(Driver(org_id=org.id,user_id=user.id,name=label))
    db.add_all([Approval(shipper_id=shipper.id,carrier_id=c1.id,active=True),
                Approval(shipper_id=shipper.id,carrier_id=c2.id,active=True)])
    for org,label in ((c1,'Тестовый ТС Б-01'),(c2,'Тестовый ТС В-01')):
        db.add(Vehicle(org_id=org.id,label=label,kind='Автоцистерна',capacity=25))

print('\nКОНФИДЕНЦИАЛЬНО. Учебные тестовые учётные записи созданы:')
for label, address, pw in accounts:
    print(f'{label:<18} {address:<34} пароль: {pw}')
print('Пять паролей показаны только сейчас. Не делайте скриншот и не публикуйте вывод.')
print('После теста отключите или замените эти учётные записи. Только вымышленные заявки.')
