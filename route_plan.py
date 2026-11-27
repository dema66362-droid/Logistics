"""Planned stops and driver-reported progress. Times are local to the dispatch board."""
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import heapq
import json
from typing import Literal
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

import app as c
from board_fleet import DAILY_KM, distance, norm

router = APIRouter()
# Planning corridors. Points are cities/approaches, not compulsory stops or navigation instructions.
CORRIDORS = (
    'Орск|Оренбург|Бузулук|Самара',
    'Москва|Рязань|Пенза|Сызрань|Самара|Уфа|Челябинск',
    'Москва|Владимир|Нижний Новгород|Казань|Уфа',
    'Оренбург|Уфа', 'Орск|Магнитогорск|Челябинск',
    'Москва|Тверь|Великий Новгород|Санкт-Петербург',
    'Москва|Воронеж|Ростов-на-Дону|Краснодар|Новороссийск',
    'Челябинск|Екатеринбург', 'Челябинск|Курган|Ишим|Омск|Новосибирск',
)

@lru_cache(maxsize=1)
def corridors():
    graph={}
    for line in CORRIDORS:
        chain=line.split('|')
        for a,b in zip(chain,chain[1:]):
            km,_=distance(a,b,{})
            if km:
                graph.setdefault(norm(a),[]).append((km,b))
                graph.setdefault(norm(b),[]).append((km,a))
    return graph

@lru_cache(maxsize=1024)
def proposed_cities(start, end):
    graph=corridors()
    if norm(start) in graph and norm(end) in graph and norm(start)!=norm(end):
        queue=[(0,start,[])]
        visited=set()
        while queue:
            cost,city,path=heapq.heappop(queue)
            key=norm(city)
            if key==norm(end):
                return [start,*path[1:],end]
            if key in visited:continue
            visited.add(key)
            for km,next_city in graph[key]:
                if norm(next_city) not in visited:
                    heapq.heappush(queue,(cost+km,next_city,[*path,city]))
    return [start,end]

def stamp(value):
    return value.isoformat(timespec='minutes')

def dt(value):
    return datetime.fromisoformat(value)

def distance_book(db,cid):
    return {(r.origin,r.destination):r.km for r in db.scalars(select(c.RoadDistance).where(c.RoadDistance.carrier_id==cid))}

def route_for(s,assignment,overrides):
    manual=isinstance(assignment,c.InternalTrip)
    day=s.pickup_date if manual else assignment.pickup_date
    time=s.pickup_time if manual else assignment.pickup_time
    due=s.unload_date if manual else assignment.delivery_day or s.unload_date
    start=dt(f'{day}T{time}')
    anchor=[s.from_city,s.to_city,stamp(start),str(due),assignment.driver_id,assignment.vehicle_id]
    saved=json.loads(assignment.route_data or '{}')
    if saved.get('anchor')!=anchor:saved={}
    cities=saved.get('cities') or proposed_cities(s.from_city,s.to_city)
    legs=[distance(a,b,overrides) for a,b in zip(cities,cities[1:])]
    stops=[]
    at=start
    for i,city in enumerate(cities):
        km,method=legs[i-1] if i else (0,'погрузка')
        if i:
            if km is None:
                # Only the contractual endpoint time is available for an unknown pair.
                at=max(at+timedelta(hours=1),dt(f'{due}T{time}'))
            else:
                at+=timedelta(minutes=max(15,round(km/DAILY_KM*1440)))
        eta=(saved.get('times') or {}).get(str(i))
        if eta:at=dt(eta)
        stops.append(dict(city=city,eta=stamp(at),km=km,method=method))
    version=hashlib.sha256(json.dumps([anchor,saved,stops],sort_keys=True).encode()).hexdigest()[:20]
    return dict(anchor=anchor,stops=stops,events=saved.get('events',[]),version=version,
                totalKm=sum(k for k,_ in legs) if all(k is not None for k,_ in legs) else None,
                dailyKm=DAILY_KM,editable=assignment.status in ('selected','driver_ok') and s.status not in ('completed','cancelled'),
                timezone=str(c.BUSINESS_TZ),due=str(due))

class RouteChange(BaseModel):
    version:str=Field(max_length=32)
    action:Literal['eta','via','arrive','depart']
    index:int=Field(ge=0,le=65)
    eta:datetime|None=None
    city:str=Field(default='',max_length=120)

def resource(db,key,user):
    if key.startswith('Q-') and key[2:].isdigit():
        assignment=db.get(c.Offer,int(key[2:]))
    else:
        assignment=db.scalar(select(c.InternalTrip).join(c.Shipment).where(c.Shipment.ref==key))
    if not assignment or user['role']!='driver' or assignment.driver_id!=c.leave_owner(db,user).id:c.forbidden()
    s=db.get(c.Shipment,assignment.shipment_id)
    if s.status in ('completed','cancelled') or assignment.status not in ('driver_ok','selected'):c.invalid('Рейс закрыт для изменений')
    return s,assignment

@router.put('/api/driver/routes/{key}')
def change_route(key:str,payload:RouteChange,request:Request,user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf'])
    with c.db_write() as db:
        s,assignment=resource(db,key,user)
        book=distance_book(db,assignment.carrier_id)
        route=route_for(s,assignment,book)
        if payload.version!=route['version']:c.invalid('График изменился. Откройте маршрут ещё раз.')
        stops=route['stops'];i=payload.index
        if i>=len(stops):c.invalid('Пункт отсутствует',422)
        cities=[p['city'] for p in stops]
        times={str(n):p['eta'] for n,p in enumerate(stops)}
        events=route['events'];last=events[-1] if events else None
        if payload.action in ('eta','via') and last and i<=last['index']:c.invalid('Этот участок уже пройден')
        if payload.action=='eta':
            if not i or not payload.eta or payload.eta.tzinfo or not 2000<=payload.eta.year<=2100:c.invalid('Укажите местные дату и время прибытия',422)
            if payload.eta<=dt(stops[i-1]['eta']):c.invalid('Прибытие должно быть позже предыдущего пункта',422)
            delta=payload.eta-dt(stops[i]['eta'])
            for n in range(i,len(stops)):times[str(n)]=stamp(dt(stops[n]['eta'])+delta)
        elif payload.action=='via':
            city=payload.city.strip()
            if not i or len(stops)>=64 or len(city)<2 or norm(city) in (norm(cities[i-1]),norm(cities[i])):c.invalid('Проверьте промежуточный город',422)
            before=dt(times[str(i-1)]);after=dt(times[str(i)])
            if after-before<timedelta(minutes=2):c.invalid('Сначала увеличьте интервал прибытия',422)
            a,_=distance(cities[i-1],city,book);b,_=distance(city,cities[i],book)
            ratio=a/(a+b) if a and b else .5
            between=max(before+timedelta(minutes=1),min(after-timedelta(minutes=1),before+(after-before)*ratio))
            cities.insert(i,city)
            times={str(n):v for n,v in enumerate([*[p['eta'] for p in stops[:i]],stamp(between),*[p['eta'] for p in stops[i:]]])}
        else:
            expected=(0,'arrive') if not last else ((last['index'],'depart') if last['action']=='arrive' else (last['index']+1,'arrive'))
            if (i,payload.action)!=expected or (i==len(stops)-1 and payload.action=='depart'):c.invalid('Сначала отметьте предшествующий этап')
            if assignment.status!='selected':c.invalid('Отметки доступны после назначения')
            events=[*events,{'index':i,'action':payload.action,'at':stamp(datetime.now(c.BUSINESS_TZ).replace(tzinfo=None))}]
        assignment.route_data=json.dumps(dict(anchor=route['anchor'],cities=cities,times=times,events=events),ensure_ascii=False)
        c.note_org(db,assignment.carrier_id,'carrier',s.id,'route_progress','Обновлён маршрут водителя',f'{s.ref} · {cities[i]}')
        return {'ok':True,'route':route_for(s,assignment,book)}

def attach_routes(db,rows):
    jobs=[j for row in rows for j in row['jobs']]
    regular={int(j['offer']['id'][2:]) for j in jobs if not j.get('internal')}
    private={j['order']['id'] for j in jobs if j.get('internal')}
    pairs={f'Q-{a.id}':(s,a) for s,a in db.execute(select(c.Shipment,c.Offer).join(c.Offer,c.Offer.shipment_id==c.Shipment.id).where(c.Offer.id.in_(regular)))}
    pairs.update({s.ref:(s,a) for s,a in db.execute(select(c.Shipment,c.InternalTrip).join(c.InternalTrip).where(c.Shipment.ref.in_(private)))})
    books={cid:distance_book(db,cid) for cid in {a.carrier_id for s,a in pairs.values()}}
    for job in jobs:
        s,a=pairs[job['offer']['id']]
        job['route']=route_for(s,a,books[a.carrier_id])
    for row in rows:
        ordered=sorted(row['jobs'],key=lambda j:j['route']['stops'][0]['eta'])
        for prev,nxt in zip(ordered,ordered[1:]):
            after=prev['route']['stops'][-1];before=nxt['route']['stops'][0]
            if norm(after['city'])!=norm(before['city']):
                cid=pairs[nxt['offer']['id']][1].carrier_id
                km,method=distance(after['city'],before['city'],books[cid])
                end=dt(before['eta'])
                start=dt(after['eta'])
                if km is not None:
                    start=max(start,end-timedelta(minutes=max(15,round(km/DAILY_KM*1440))))
                nxt['emptyApproach']=dict(origin=after['city'],dest=before['city'],start=stamp(start),end=before['eta'],km=km,method=method)
