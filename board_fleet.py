"""Opt-in fleet visibility and conservative backhaul estimates in the carrier's own pool."""
import csv
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter,Depends,Request
from pydantic import BaseModel,Field
from sqlalchemy import select

import app as c
from board_partners import connected

router=APIRouter()
DEFAULT_COSTS={'empty_km':32.0,'loaded_km':44.0,'wait_day':4000.0}
DAILY_KM=550


def norm(value):
    return re.sub(r'^(?:город\s+|г\.\s*)','',value.strip().casefold().replace('ё','е'))


@lru_cache(maxsize=1)
def cities():
    data=defaultdict(list)
    with (c.BASE/'docs'/'cities.csv').open(encoding='utf-8-sig') as source:
        for row in csv.DictReader(source):
            coords=(float(row['lat']),float(row['lon']))
            for alias in {row['name'],*row['aliases'].split('|')} - {''}:
                for key in (alias,alias+', '+row['region'],alias+', '+row['country']):
                    data[norm(key)].append(coords)
    return {key:points[0] for key,points in data.items() if len(set(points))==1}


def distance(a,b,overrides):
    a,b=norm(a),norm(b)
    if (a,b) in overrides:return overrides[a,b],'по маршруту перевозчика'
    if a==b:return 0,'в пределах указанного города'
    source,dest=cities().get(a),cities().get(b)
    if source is None or dest is None:return None,'расстояние не задано'
    lat1,lon1,lat2,lon2=map(math.radians,(*source,*dest))
    value=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return round(6371*2*math.asin(min(1,math.sqrt(value)))*1.25),'оценка между центрами городов'


class Costs(BaseModel):
    empty_km:Decimal=Field(gt=0,le=5000,decimal_places=2)
    loaded_km:Decimal=Field(gt=0,le=5000,decimal_places=2)
    wait_day:Decimal=Field(ge=0,le=500000,decimal_places=2)


class Pair(BaseModel):
    origin:str=Field(min_length=2,max_length=120)
    destination:str=Field(min_length=2,max_length=120)
    km:int=Field(ge=0,le=40000)


class Position(BaseModel):
    vehicle_id:int
    city:str=Field(min_length=2,max_length=120)
    home:str=Field(min_length=2,max_length=120)
    available:date


class Share(BaseModel):
    partner_id:int
    subject:Literal['company','driver']='company'
    enabled:bool


def carrier(user):
    if user['role']!='carrier':c.forbidden()
    return user['org_id']


def cost_info(db,cid):
    row=db.get(c.CarrierCosts,cid)
    return ({k:float(getattr(row,k)) for k in DEFAULT_COSTS} if row else dict(DEFAULT_COSTS),bool(row))


@router.get('/api/fleet/settings')
def settings(user=Depends(c.user_session)):
    cid=carrier(user)
    with c.SessionLocal() as db:
        values,custom=cost_info(db,cid)
        return {'costs':values,'custom':custom,'positions':[dict(vehicle=p.vehicle_id,city=p.city,home=p.home,available=p.available)
            for p in db.scalars(select(c.VehiclePosition).join(c.Vehicle).where(c.Vehicle.org_id==cid))],
            'distances':[{'origin':p.origin,'destination':p.destination,'km':p.km} for p in db.scalars(
                select(c.RoadDistance).where(c.RoadDistance.carrier_id==cid))]}


@router.post('/api/fleet/costs')
def save_costs(payload:Costs,request:Request,user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf']);cid=carrier(user)
    with c.db_write() as db:db.merge(c.CarrierCosts(carrier_id=cid,**payload.model_dump()))
    return {'ok':True}


@router.post('/api/fleet/distance')
def save_distance(payload:Pair,request:Request,user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf']);cid=carrier(user)
    a,b=norm(payload.origin),norm(payload.destination)
    if len(a)<2 or len(b)<2 or (a!=b and not payload.km):c.invalid('Проверьте города и пробег',422)
    with c.db_write() as db:db.merge(c.RoadDistance(carrier_id=cid,origin=a,destination=b,km=payload.km))
    return {'ok':True}


@router.post('/api/fleet/position')
def set_position(payload:Position,request:Request,user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf']);cid=carrier(user)
    with c.db_write() as db:
        v=db.get(c.Vehicle,payload.vehicle_id)
        if not v or v.org_id!=cid:c.forbidden()
        db.merge(c.VehiclePosition(vehicle_id=v.id,city=c.clean_label(payload.city),home=c.clean_label(payload.home),available=payload.available))
    return {'ok':True}


@router.post('/api/fleet/share')
def grant(payload:Share,request:Request,user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf']);cid=carrier(user)
    with c.db_write() as db:
        if payload.subject=='driver':
            person=db.get(c.Driver,payload.partner_id)
            if not person or not person.user_id or not c.carrier_has_driver(db,cid,person):c.forbidden()
            existing=db.get(c.DriverFleetGrant,(cid,person.id))
            if payload.enabled and not existing:db.add(c.DriverFleetGrant(carrier_id=cid,driver_id=person.id))
            if not payload.enabled and existing:db.delete(existing)
            return {'ok':True}
        org=db.get(c.Organization,payload.partner_id)
        if not org or org.disposition!='active' or not connected(db,cid,org.id):c.forbidden()
        existing=db.get(c.FleetGrant,(cid,org.id))
        if payload.enabled and not existing:db.add(c.FleetGrant(carrier_id=cid,partner_id=org.id))
        if not payload.enabled and existing:db.delete(existing)
    return {'ok':True}


def trips(db,cid):
    result=[]
    committed=select(c.Shipment,c.Offer).join(c.Offer,c.Offer.id==c.Shipment.selected_offer_id).where(
        c.Offer.carrier_id==cid,c.Offer.status=='selected',c.Shipment.status.in_(['confirmed','enroute','completed']))
    for s,q in db.execute(committed):
        result.append((q.vehicle_id,q.driver_id,s.id,c.official_slot(s,q)[0],q.end_date,s.from_city,s.to_city))
    for s,t in db.execute(select(c.Shipment,c.InternalTrip).join(c.InternalTrip).where(
            c.InternalTrip.carrier_id==cid,c.InternalTrip.status.in_(['selected','completed']))):
        result.append((t.vehicle_id,t.driver_id,s.id,s.pickup_date,s.unload_date,s.from_city,s.to_city))
    return result


@router.get('/api/fleet/shared/{carrier_id}')
def shared(carrier_id:int,week:date,user=Depends(c.user_session)):
    start=week-timedelta(days=week.weekday());end=start+timedelta(days=6)
    with c.SessionLocal() as db:
        carrier_org=db.get(c.Organization,carrier_id)
        if user['role']=='driver':
            person=db.scalar(select(c.Driver).where(c.Driver.user_id==user['id']))
            granted=person and c.carrier_has_driver(db,carrier_id,person) and db.get(c.DriverFleetGrant,(carrier_id,person.id))
        else:granted=db.get(c.FleetGrant,(carrier_id,user['org_id'])) and connected(db,carrier_id,user['org_id'])
        if not granted or not carrier_org or carrier_org.disposition!='active':c.forbidden('Перевозчик не открыл вам график')
        return [{'vehicle':db.get(c.Vehicle,v).label,'start':a,'end':b,'from':origin,'to':dest}
                for v,_,_,a,b,origin,dest in sorted(trips(db,carrier_id),key=lambda x:(x[3],x[0])) if a<=end and b>=start]


def metric(anchor,order,start,unload,price,overrides,cost):
    available,city,home,unknown_hour=anchor
    idle=(start-available).days
    approach,m1=distance(city,order.from_city,overrides)
    load,m2=distance(order.from_city,order.to_city,overrides)
    residual,m3=distance(order.to_city,home,overrides)
    baseline,m4=distance(city,home,overrides)
    if idle<0 or (approach is not None and approach>DAILY_KM*(idle+1)):return None
    if load is not None and load>DAILY_KM*((unload-start).days+1):return None
    metrics={'emptyKm':approach,'loadedKm':load,'homeKm':residual,'emptyHomeKm':baseline,'days':idle,
             'method':'расстояние не задано' if None in (approach,load,residual,baseline) else
                ('оценка между центрами городов' if any('оценка' in x for x in (m1,m2,m3,m4)) else 'указанные маршруты и города'),
             'margin':None,'gain':None,'profitPercent':None,'needTime':bool(unknown_hour and (idle==0 or approach and approach>DAILY_KM*idle))}
    if price>0 and None not in (approach,load,residual,baseline):
        empty=(approach+residual)*cost['empty_km']
        wait=max(0,idle-math.ceil(approach/DAILY_KM))*cost['wait_day']
        expense=empty+load*cost['loaded_km']+wait
        margin=round(price-expense)
        metrics.update(margin=margin,gain=round(margin+baseline*cost['empty_km']),cost=round(expense),
                       profitPercent=round(100*margin/price,1))
    return metrics


def matches(db,cid,orders,shared_mode=False):
    cost,custom=cost_info(db,cid)
    overrides={(r.origin,r.destination):r.km for r in db.scalars(select(c.RoadDistance).where(c.RoadDistance.carrier_id==cid))}
    vehicles={v.id:v for v in db.scalars(select(c.Vehicle).where(c.Vehicle.org_id==cid))}
    pos={p.vehicle_id:p for p in db.scalars(select(c.VehiclePosition).join(c.Vehicle).where(c.Vehicle.org_id==cid))}
    by_vehicle=defaultdict(list)
    for job in trips(db,cid):by_vehicle[job[0]].append(job)
    results=[];company=db.get(c.Organization,cid)
    for order in orders:
        if not c.can_receive(db,order,cid) and not c.public_visible(db,order,cid):continue
        active=db.scalar(select(c.Offer).where(c.Offer.shipment_id==order.id,c.Offer.carrier_id==cid,c.Offer.status.in_(('driver_ok','selected','waiting_driver'))).limit(1))
        if active and not shared_mode:continue
        start,time=c.official_slot(order);unload=order.unload_date
        if start<c.business_today():continue
        for vid,v in vehicles.items():
            if v.kind!=order.vehicle_kind or v.capacity<order.tons or (active and active.vehicle_id!=vid):continue
            jobs=by_vehicle[vid]
            prev=max((j for j in jobs if j[4]<=start),key=lambda j:(j[4],j[3],j[2]),default=None)
            stop=pos.get(vid)
            if stop and stop.available>start and (not prev or stop.available>prev[4]):continue
            if stop and stop.available<=start and (not prev or stop.available>prev[4]):
                anchor=(stop.available,stop.city,stop.home,False);did=-1;exclude=None
            elif prev:
                anchor=(prev[4],prev[6],stop.home if stop else prev[5],True);did=prev[1];exclude=prev[2]
            else:continue
            if c.conflicts(db,did,vid,start,unload,exclude_shipment=exclude,include_driver_accepted=True):
                if not active:continue
                # A matching bid holds its own truck. Other bookings stay disqualifying.
                if c.conflicts(db,did,vid,start,unload,exclude_shipment=order.id,include_driver_accepted=True):continue
            price=active.price if active else order.budget
            m=metric(anchor,order,start,unload,price,overrides,cost)
            if m is None:continue
            nxt=min((j for j in jobs if j[3]>unload),key=lambda j:j[3],default=None)
            if nxt:
                km,_=distance(order.to_city,nxt[5],overrides)
                if km is None or km>DAILY_KM*(nxt[3]-unload).days:continue
            common=dict(order=order.ref,vehicle=v.label,carrier=company.name,fromCity=order.from_city,toCity=order.to_city,
                start=start.isoformat(),time=time,availableCity=anchor[1],availableDay=anchor[0].isoformat(),
                price=price if (shared_mode or c.allowed(db,order,cid)) else None,
                emptyKm=m['emptyKm'],days=m['days'],needTime=m['needTime'],crewKnown=did!=-1,method=m['method'])
            if shared_mode:
                common['mutual']=bool(m['gain'] is not None and m['gain']>0 and m['margin']>0)
                common['offeredPrice']=active.price if active else None
            else:
                if not c.allowed(db,order,cid):continue
                common.update(m,homeCity=anchor[2],customCosts=custom)
            results.append(common)
    return results


@router.get('/api/fleet/matches')
def recommendations(user=Depends(c.user_session)):
    if user['role'] not in ('shipper','carrier'):c.forbidden()
    with c.SessionLocal() as db:
        query=select(c.Shipment).where(c.Shipment.status.in_(['search','choice']),
            c.Shipment.hazardous.is_(False),c.Shipment.selected_offer_id.is_(None),c.Shipment.delete_after.is_(None))
        oid=user['org_id']
        if user['role']=='carrier':
            accounts=select(c.Approval.shipper_id).join(c.Organization,c.Organization.id==c.Approval.shipper_id).where(
                c.Approval.carrier_id==oid,c.Approval.active.is_(True),c.Organization.disposition=='active')
            results=matches(db,oid,db.scalars(query.where(c.Shipment.search_launched.is_(True),c.Shipment.shipper_id.in_(accounts))).all())
            results.sort(key=lambda m:(m['gain'] is None,m['gain'] is None or m['gain']<=0,
                m['needTime'],m['days'],-(m['gain'] or 0),m['emptyKm'] if m['emptyKm'] is not None else math.inf,m['order']))
        else:
            own=db.scalars(query.where(c.Shipment.shipper_id==oid)).all();results=[]
            for cid in db.scalars(select(c.FleetGrant.carrier_id).join(c.Organization,c.Organization.id==c.FleetGrant.carrier_id).where(
                    c.FleetGrant.partner_id==oid,c.Organization.disposition=='active')):
                if connected(db,cid,oid):results.extend(matches(db,cid,own,True))
            results.sort(key=lambda m:(m['start'],m['time'],m['order'],m['needTime'],m['offeredPrice'] is None,
                m['offeredPrice'] if m['offeredPrice'] is not None else math.inf,
                m['emptyKm'] if m['emptyKm'] is not None else math.inf,not m['mutual'],m['days']))
        return {'items':results,'dailyKm':DAILY_KM}
