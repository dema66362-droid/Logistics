"""Carrier crew directory and acknowledgements of a specific trip reschedule."""
import re
from decimal import Decimal
from typing import Literal
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
import app as c

router = APIRouter()


class CrewIn(BaseModel):
    driver_id: int | None = None
    name: str = Field(default='', max_length=100)
    tractor: str = Field(min_length=3, max_length=20)
    trailer: str = Field(min_length=3, max_length=20)
    kind: Literal['Автоцистерна', 'Фура']
    capacity: Decimal = Field(gt=0, le=120, decimal_places=2)
    legacy_vehicle: int | None = None


def plate(value):
    # Normalise Latin/Cyrillic look-alikes so the same physical unit cannot be booked twice.
    value = re.sub(r'[\s-]', '', value.upper()).translate(str.maketrans('АВЕКМНОРСТУХ','ABEKMHOPCTYX'))
    if not re.fullmatch(r'[A-ZА-ЯЁ0-9]{3,16}', value) or not any(x.isdigit() for x in value):
        c.invalid('Проверьте госномер и код региона', 422)
    return value


def visible_crews(db, user, drivers):
    if not drivers:
        return []
    by_id = {d.id:d for d in drivers}
    query = select(c.CrewPreference,c.Vehicle,c.Organization).join(
        c.Vehicle,c.Vehicle.id==c.CrewPreference.vehicle_id).join(
        c.Organization,c.Organization.id==c.CrewPreference.carrier_id).where(
        c.CrewPreference.driver_id.in_(by_id))
    if user['role']=='carrier':
        query = query.where(c.CrewPreference.carrier_id==user['org_id'])
    return [dict(driverId=p.driver_id, name=by_id[p.driver_id].name, vehicleId=v.id,
                 tractor=v.tractor, trailer=v.trailer, kind=v.kind, capacity=float(v.capacity),
                 company=org.name) for p,v,org in db.execute(query.order_by(c.Organization.id,c.CrewPreference.driver_id))
            if org.disposition=='active' and c.carrier_has_driver(db,org.id,by_id[p.driver_id])]


@router.post('/api/carrier/crew')
def save_crew(data:CrewIn, request:Request, user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf'])
    if user['role']!='carrier':
        c.forbidden()
    cid = user['org_id']
    tractor, trailer = plate(data.tractor), plate(data.trailer)
    if tractor==trailer:
        c.invalid('Укажите разные номера тягача и прицепа',422)
    with c.db_write() as db:
        if data.driver_id is not None:
            driver = db.get(c.Driver,data.driver_id)
            if not driver or not c.carrier_has_driver(db,cid,driver):
                c.forbidden()
        else:
            name = c.clean_label(data.name)
            same = [d for d in c.carrier_drivers(db,cid) if d.name.casefold()==name.casefold()]
            if len(same)>1:
                c.invalid('Выберите водителя из списка однофамильцев')
            driver = same[0] if same else None
            if not driver:
                driver = c.Driver(org_id=cid,name=name)
                db.add(driver)
                db.flush()
                db.add(c.RosterMember(carrier_id=cid,driver_id=driver.id,name=name))
        pair = db.scalar(select(c.Vehicle).where(c.Vehicle.org_id==cid,c.Vehicle.tractor==tractor,
                         c.Vehicle.trailer==trailer))
        if data.legacy_vehicle:
            legacy=db.get(c.Vehicle,data.legacy_vehicle)
            if not legacy or legacy.org_id!=cid:
                c.forbidden()
            if legacy.tractor or legacy.trailer or (pair and pair.id!=legacy.id):
                c.invalid('Эта машина уже привязана к номерам')
            if legacy.kind!=data.kind or legacy.capacity!=data.capacity:
                c.invalid('Тип и грузоподъёмность должны совпадать с карточкой машины')
            pair=legacy
        if pair and pair.tractor and (pair.kind!=data.kind or pair.capacity!=data.capacity):
            c.invalid('Для этой сцепки уже указаны другие тип или грузоподъёмность')
        if not pair:
            pair=c.Vehicle(org_id=cid,kind=data.kind,capacity=data.capacity)
            db.add(pair)
        pair.tractor, pair.trailer, pair.label = tractor,trailer,f'{tractor} / {trailer}'
        db.flush()
        db.merge(c.CrewPreference(carrier_id=cid,driver_id=driver.id,vehicle_id=pair.id))
        if driver.user_id:
            c.note_user(db,driver.user_id,None,'crew_profile','Мои водители и машины',
                        f'Закреплена техника: тягач {tractor}, прицеп {trailer}')
        return {'driverId':driver.id,'vehicleId':pair.id}


def request_slot_receipts(db, shipment, *, changed_day):
    for bid in db.scalars(select(c.Offer).where(c.Offer.shipment_id==shipment.id,
                          c.Offer.status.in_(['driver_ok','selected','waiting_driver']))):
        if not changed_day and not (bid.slot_version and not (bid.carrier_ack and bid.driver_ack)):
            continue
        bid.slot_version += 1
        bid.carrier_ack = bid.driver_ack = False
        day,time=c.official_slot(shipment)
        message=f'{shipment.ref} · Подача {day:%d.%m.%Y} в {time}. Подтвердите новую дату или предложите другую.'
        c.note_org(db,bid.carrier_id,'carrier',shipment.id,'slot_request','Заказчик перенёс рейс',message)
        driver=db.get(c.Driver,bid.driver_id)
        if driver.user_id:
            c.note_user(db,driver.user_id,shipment.id,'slot_request','Заказчик перенёс рейс',message)


class Receipt(BaseModel):
    version: int = Field(ge=1)


@router.post('/api/offers/{offer_id}/confirm-date')
def confirm_date(offer_id:int, payload:Receipt, request:Request, user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf'])
    with c.db_write() as db:
        bid=db.get(c.Offer,offer_id)
        if not bid:
            c.forbidden()
        role=user['role']
        driver=db.get(c.Driver,bid.driver_id)
        if role=='carrier' and bid.carrier_id==user['org_id']:
            field, label='carrier_ack','Перевозчик'
        elif role=='driver' and driver.user_id==user['id']:
            field, label='driver_ack','Водитель'
        else:
            c.forbidden()
        s=db.get(c.Shipment,bid.shipment_id)
        if s.status not in ('confirmed','choice','search') or bid.status not in ('selected','driver_ok') or bid.slot_version!=payload.version:
            c.invalid('Этот перенос уже не действует. Откройте актуальную карточку')
        if not getattr(bid,field):
            setattr(bid,field,True)
            day,time=c.official_slot(s)
            actor=db.get(c.User,user['id'])
            c.audit(db,actor,s,'date_acknowledged',f'{label} · {day} {time} · v{payload.version}')
            c.note_org(db,s.shipper_id,'shipper',s.id,'date_acknowledged',f'{label} подтвердил перенос',
                       f'{s.ref} · {day:%d.%m.%Y} в {time}')
        return {'carrierAck':bid.carrier_ack,'driverAck':bid.driver_ack}
