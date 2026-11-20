"""Shared shipment editor, price history and delayed cancellation."""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import delete, select, update

import app as c

router = APIRouter()
active_bids = ('waiting_driver', 'driver_ok', 'selected')


def edit_scope(db, shipment, user):
    """Only participants in this actual dispatch may change its common card."""
    role, org = user['role'], user['org_id']
    if role == 'shipper':
        return org == shipment.shipper_id
    manual = db.get(c.InternalTrip, shipment.id)
    if manual:
        return (role == 'carrier' and org == manual.carrier_id or
                role == 'driver' and db.get(c.Driver, manual.driver_id).user_id == user['id'])
    own = select(c.Offer.id).where(c.Offer.shipment_id == shipment.id)
    if shipment.selected_offer_id:
        own = own.where(c.Offer.id == shipment.selected_offer_id)
    else:
        own = own.where(c.Offer.status.in_(active_bids))
    if role == 'carrier':
        if not shipment.selected_offer_id:
            target = db.get(c.DispatchRecipient, (shipment.id, org))
            if target and not target.declined and c.allowed(db, shipment, org):
                return True
            if not c.allowed(db, shipment, org):
                return False
        return db.scalar(own.where(c.Offer.carrier_id == org).limit(1)) is not None
    if role == 'driver':
        if not shipment.selected_offer_id:
            own=own.where(c.Offer.carrier_id.in_(select(c.Approval.carrier_id).where(
                c.Approval.shipper_id==shipment.shipper_id,c.Approval.active.is_(True))))
        return db.scalar(own.join(c.Driver, c.Driver.id == c.Offer.driver_id).where(
            c.Driver.user_id == user['id']).limit(1)) is not None
    return False


def find_editable(db, ref, user):
    shipment = db.scalar(select(c.Shipment).where(c.Shipment.ref == ref))
    if not shipment or not edit_scope(db, shipment, user):
        raise HTTPException(404, detail='Заявка недоступна')
    return shipment


def price_fields(s, db):
    offer = db.get(c.Offer, s.selected_offer_id) if s.selected_offer_id else None
    current = offer.price if offer else s.budget
    return {'firstPrice': s.first_price, 'currentPrice': current,
            'priceChanged': s.price_revised or (s.first_price is not None and current != s.first_price)}


class Edit(c.CreateShipment):
    expected_revision: int = Field(ge=1)
    epd: str = Field(default='', max_length=80)
    etrn: str = Field(default='', max_length=80)
    carrier_id: int | None = None
    driver_id: int | None = None
    vehicle_id: int | None = None
    customer: str = Field(default='', max_length=160)


def crew_ids(db,s):
    selected = db.get(c.Offer, s.selected_offer_id) if s.selected_offer_id else db.get(c.InternalTrip,s.id)
    return (selected.carrier_id,selected.driver_id,selected.vehicle_id) if selected else (None,None,None)


def update_unload(db,s,day,pickup):
    if day < pickup:
        c.invalid('Выгрузка раньше погрузки',422)
    old = s.unload_date
    s.unload_date = day
    for q in db.scalars(select(c.Offer).where(c.Offer.shipment_id==s.id,c.Offer.status.in_(active_bids))):
        if q.status=='selected' or q.delivery_day is None or q.delivery_day==old:
            old_delivery=q.delivery_day or old
            q.end_date=max(day,q.end_date+(day-old_delivery))
            q.delivery_day=day


@router.get('/api/journal/{ref}/editor')
def editor(ref:str,user=Depends(c.user_session)):
    with c.SessionLocal() as db:
        s=find_editable(db,ref,user)
        if s.status=='cancelled':c.invalid('Заявка помечена на удаление')
        chosen=db.get(c.Offer,s.selected_offer_id) if s.selected_offer_id else None
        manual=db.get(c.InternalTrip,s.id)
        day,time=c.official_slot(s,chosen)
        potential=list(crew_ids(db,s))
        candidates=[]
        if potential[0] is not None:
            ids={potential[0]}
            if not manual:
                ids.update(db.scalars(select(c.Approval.carrier_id).where(c.Approval.shipper_id==s.shipper_id,c.Approval.active.is_(True))))
            for cid in sorted(ids):
                org=db.get(c.Organization,cid)
                if not org or org.disposition!='active':continue
                candidates.append({'id':cid,'name':org.name,'drivers':[{'id':d.id,'name':d.name} for d in c.carrier_drivers(db,cid)],
                    'vehicles':[{'id':v.id,'name':v.label} for v in db.scalars(select(c.Vehicle).where(c.Vehicle.org_id==cid))]})
        data={key:getattr(s,key) for key in ('from_city','to_city','recipient','product','tons','vehicle_kind','mode','manager','note',
            'pickup_company','pickup_contact','pickup_address','delivery_address','recipient_contact','terms','public_external','epd','etrn','hazardous')}
        data.update(vehicle_type=data.pop('vehicle_kind'),date=day,time=time,unload=chosen.delivery_day if chosen and chosen.delivery_day else s.unload_date,
                    budget=price_fields(s,db)['currentPrice'],tariff_per_ton=s.rate,expected_revision=s.revision,
                    customer=manual.customer if manual else '',carrier_id=potential[0],driver_id=potential[1],vehicle_id=potential[2])
        return {'order':data,'carriers':candidates,'author':s.creator_name or s.manager,'manual':bool(manual),**price_fields(s,db)}


def change_crew(db,s,user,ids,manual,selected,start,end,kind,tons):
    current=crew_ids(db,s)
    if ids==(None,None,None):
        return selected
    if not all(ids) or current[0] is None:
        c.invalid('Сначала назначьте экипаж через предложение перевозчика',422)
    cid,did,vid=ids
    if manual and cid!=manual.carrier_id or not manual and cid!=current[0] and not c.allowed(db,s,cid):
        c.forbidden('Выберите перевозчика из подтверждённых партнёров')
    d=db.get(c.Driver,did);v=db.get(c.Vehicle,vid);org=db.get(c.Organization,cid)
    if not org or org.disposition!='active' or not d or not c.carrier_has_driver(db,cid,d) or not v or v.org_id!=cid:
        c.invalid('Экипаж отсутствует в пуле выбранного перевозчика',422)
    if v.kind!=kind or v.capacity<tons:c.invalid('Тип или грузоподъёмность машины не соответствуют заявке')
    if s.status!='completed' and c.conflicts(db,did,vid,start,end,exclude_shipment=s.id,include_driver_accepted=True):
        c.invalid('Водитель или машина заняты в эти даты')
    if ids==current:return selected
    old_driver=db.get(c.Driver,current[1]);previous_cid=current[0]
    actor=db.get(c.User,user['id'])
    c.note_org(db,previous_cid,'carrier',s.id,'order_updated','Экипаж изменён',s.ref+' · Назначение изменено')
    if old_driver.user_id:c.note_user(db,old_driver.user_id,s.id,'order_updated','График изменён',s.ref+' · Назначение изменено')
    if manual:
        manual.driver_id,manual.vehicle_id=did,vid
    else:
        selected.status='replaced'
        selected=c.Offer(shipment_id=s.id,carrier_id=cid,driver_id=did,vehicle_id=vid,price=selected.price,
            pickup_date=start,pickup_time=c.official_slot(s,selected)[1],delivery_day=s.unload_date,end_date=end,
            status='selected',terms_accepted=True,accepted_terms_version=s.terms_version)
        db.add(selected);db.flush();s.selected_offer_id=selected.id
    return selected


@router.put('/api/journal/{ref}')
def edit(ref:str,payload:Edit,request:Request,user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf'])
    with c.db_write() as db:
        s=find_editable(db,ref,user)
        if s.status=='cancelled':c.invalid('Заявка помечена на удаление')
        if s.revision!=payload.expected_revision:c.invalid('Другой участник уже изменил заявку. Откройте карточку заново')
        manual=db.get(c.InternalTrip,s.id)
        selected=db.get(c.Offer,s.selected_offer_id) if s.selected_offer_id else None
        current=c.official_slot(s,selected)
        day,time=payload.date,payload.time
        unload=payload.unload or day
        # Keep historical dates valid when only contact details or pricing are corrected.
        check=payload.model_copy()
        if day<c.business_today():
            if day!=current[0] and s.status!='completed':c.invalid('Новая дата погрузки уже прошла',422)
            if unload<day:c.invalid('Выгрузка раньше погрузки',422)
            check.date=c.business_today();check.unload=check.date+(unload-day)
        validated=c.build_shipment(check,user)
        if validated.hazardous and (s.search_launched or selected or manual):
            c.invalid('Для этого груза создайте заявку на проверку допуска')
        if payload.public_external!=s.public_external and (s.search_launched or user['role']!='shipper'):
            c.invalid('Аудиторию можно выбрать до отправки заявки')
        changed_slot=(day,time)!=current
        pending=changed_slot and user['role']!='shipper' and not manual
        if pending and s.status not in ('search','choice','confirmed'):c.invalid('Согласование времени для закрытого рейса недоступно')
        old_price=price_fields(s,db)['currentPrice']
        if s.first_price is None and old_price:s.first_price=old_price
        old_unload=selected.delivery_day if selected and selected.delivery_day else s.unload_date
        actual_start=current[0] if pending else day
        actual_end=old_unload if pending else unload
        crew=(payload.carrier_id,payload.driver_id,payload.vehicle_id)
        hold_tail=max(timedelta(0),selected.end_date-old_unload) if selected else timedelta(0)
        selected=change_crew(db,s,user,crew,manual,selected,actual_start,actual_end+hold_tail,validated.vehicle_kind,validated.tons)
        if not pending and unload!=old_unload:update_unload(db,s,unload,day)
        if pending:
            bid=selected
            if not bid:
                query=select(c.Offer).where(c.Offer.shipment_id==s.id,c.Offer.status.in_(active_bids))
                if user['role']=='driver':query=query.join(c.Driver).where(c.Driver.user_id==user['id'])
                else:query=query.where(c.Offer.carrier_id==user['org_id'])
                bid=db.scalar(query.limit(1))
            cid=bid.carrier_id if bid else user['org_id']
            prop,_=c.save_proposal(db,s,db.get(c.User,user['id']),cid,bid,day,time)
            prop.new_unload=unload if unload!=old_unload else None
        elif changed_slot:
            if not manual and s.status!='completed':c.update_slot(db,s,day,time)
            else:
                s.slot_day,s.slot_time=day,time
                if selected:selected.pickup_date,selected.pickup_time=day,time
            if manual:s.pickup_date,s.pickup_time=day,time
            db.execute(update(c.SlotProposal).where(c.SlotProposal.shipment_id==s.id,c.SlotProposal.status=='pending').values(status='superseded'))
        for field in ('from_city','to_city','recipient','product','tons','vehicle_kind','mode','note','pickup_company','pickup_contact',
                      'pickup_address','delivery_address','recipient_contact','terms','public_external','hazardous','rate','budget'):
            setattr(s,field,getattr(validated,field))
        s.manager=payload.manager.strip();s.epd=payload.epd.strip();s.etrn=payload.etrn.strip()
        if manual:manual.customer=c.clean_label(payload.customer)
        if old_price!=s.budget:
            if s.first_price is None and s.budget:s.first_price=s.budget
            elif s.first_price is not None:s.price_revised=True
        if selected:
            if s.budget<=0:c.invalid('Для назначенного рейса укажите стоимость выше нуля',422)
            selected.price=s.budget
        c.audit(db,db.get(c.User,user['id']),s,'slot_proposed' if pending else 'order_updated',
                'Редактор заявки · '+user['name'])
        return {'ok':True,'pendingSlot':pending,'revision':s.revision,**price_fields(s,db)}


def cancel(ref,request,user):
    c.protect_csrf(request,user['csrf'])
    with c.db_write() as db:
        s=db.scalar(select(c.Shipment).where(c.Shipment.ref==ref))
        if not s:raise HTTPException(404,detail='Заявка недоступна')
        notified=s.delete_after and db.scalar(select(c.Notification.id).where(c.Notification.user_id==user['id'],
            c.Notification.shipment_id==s.id,c.Notification.kind=='order_cancelled').limit(1))
        if not notified and not edit_scope(db,s,user):c.forbidden('Эта заявка принадлежит другим участникам')
        if s.delete_after:return {'ok':True,'deleteAfter':s.delete_after}
        s.delete_after=datetime.now(timezone.utc)+timedelta(days=1)
        # Notify all previously reached parties before closing their offers.
        c.audit(db,db.get(c.User,user['id']),s,'order_cancelled','Помечена на удаление через 24 часа')
        s.status='cancelled';s.search_launched=False
        t=db.get(c.InternalTrip,s.id)
        if t:t.status='cancelled'
        db.execute(update(c.Offer).where(c.Offer.shipment_id==s.id,c.Offer.status.in_(active_bids)).values(status='cancelled'))
        db.execute(update(c.SlotProposal).where(c.SlotProposal.shipment_id==s.id,c.SlotProposal.status=='pending').values(status='superseded'))
        return {'ok':True,'deleteAfter':s.delete_after}


@router.get('/api/journal-deletions')
def cancellations(user=Depends(c.user_session)):
    now=datetime.now(timezone.utc)
    with c.SessionLocal() as db:
        records=db.scalars(select(c.Shipment).where(c.Shipment.delete_after>now).order_by(c.Shipment.delete_after))
        return [{'ref':s.ref,'route':s.from_city+' → '+s.to_city,'at':s.delete_after.replace(tzinfo=timezone.utc)}
                for s in records if edit_scope(db,s,user) or db.scalar(select(c.Notification.id).where(
                    c.Notification.user_id==user['id'],c.Notification.shipment_id==s.id,c.Notification.kind=='order_cancelled').limit(1))]


def cleanup(now=None):
    now=now or datetime.now(timezone.utc)
    with c.SessionLocal() as db:
        due=db.scalar(select(c.Shipment.id).where(c.Shipment.delete_after<=now).limit(1))
        legacy=db.scalar(select(c.Offer.id).where(c.Offer.status=='waiting_driver').limit(1)) or db.scalar(
            select(c.InternalTrip.shipment_id).where(c.InternalTrip.status=='waiting_driver').limit(1))
    if not due and not legacy:return
    with c.db_write() as db:
        ids=list(db.scalars(select(c.Shipment.id).where(c.Shipment.delete_after<=now)))
        if ids:
            db.execute(update(c.Notification).where(c.Notification.shipment_id.in_(ids)).values(shipment_id=None))
            for table in (c.SlotProposal,c.DispatchRecipient,c.InternalTrip,c.Offer,c.Audit):
                db.execute(delete(table).where(table.shipment_id.in_(ids)))
            db.execute(delete(c.Shipment).where(c.Shipment.id.in_(ids)))
        for q in db.scalars(select(c.Offer).where(c.Offer.status=='waiting_driver').order_by(c.Offer.id)):
            s=db.get(c.Shipment,q.shipment_id)
            free=s.status in ('choice','search') and not c.conflicts(db,q.driver_id,q.vehicle_id,q.pickup_date,q.end_date,
                                                                    exclude_shipment=s.id,include_driver_accepted=True)
            q.status='driver_ok' if free else 'withdrawn'
            if free:s.status='choice'
            c.note_org(db,q.carrier_id,'carrier',s.id,'crew_reserved','График обновлён',
                       s.ref+(' · Экипаж зарезервирован' if free else ' · Нужна замена экипажа'))
            driver=db.get(c.Driver,q.driver_id)
            if driver.user_id:c.note_user(db,driver.user_id,s.id,'crew_reserved','График обновлён',s.ref)
            db.flush()
        for t in db.scalars(select(c.InternalTrip).where(c.InternalTrip.status=='waiting_driver').order_by(c.InternalTrip.shipment_id)):
            s=db.get(c.Shipment,t.shipment_id)
            if c.conflicts(db,t.driver_id,t.vehicle_id,s.pickup_date,s.unload_date,exclude_shipment=s.id,include_driver_accepted=True):
                t.status='declined';s.status='cancelled'
                c.note_org(db,t.carrier_id,'carrier',s.id,'schedule_conflict','Требуется замена экипажа',s.ref)
            else:t.status='selected';s.status='confirmed'
            db.flush()


async def housekeeping():
    while True:
        try:await asyncio.to_thread(cleanup)
        except Exception:logging.getLogger(__name__).exception('Scheduled shipment cleanup failed')
        await asyncio.sleep(30)
