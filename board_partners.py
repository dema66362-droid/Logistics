"""Company directory, bilateral partnerships and named employee accounts."""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select

import app as c

router=APIRouter()


def connected(db,first,second):
    if first==second:return False
    pair=tuple(sorted((first,second)))
    return bool(db.get(c.CompanyPeer,pair) or db.scalar(select(c.Approval.id).where(c.Approval.active.is_(True),
        ((c.Approval.shipper_id==first)&(c.Approval.carrier_id==second))|
        ((c.Approval.shipper_id==second)&(c.Approval.carrier_id==first))).limit(1)))


@router.get('/api/partners/search')
def search(q:str='',user=Depends(c.user_session)):
    query=q.strip().casefold().replace('ё','е')
    if len(query)<4 or len(query)>160:return []
    with c.SessionLocal() as db:
        stmt=select(c.Organization).where(c.Organization.kind.in_(['shipper','carrier']),
            c.Organization.disposition=='active',c.Organization.id!=user['org_id'])
        condition=(c.Organization.inn.startswith(query,autoescape=True) if query.isdigit()
                   else c.Organization.directory_key.contains(query,autoescape=True))
        return [{'id':o.id,'name':o.name,'inn':o.inn,'role':o.kind}
                for o in db.scalars(stmt.where(condition).order_by(c.Organization.name,c.Organization.id).limit(20))]


class CompanyTarget(BaseModel):
    company_id:int=Field(gt=0)


@router.post('/api/partners/invite',status_code=201)
def invite_company(payload:CompanyTarget,request:Request,response:Response,user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf'])
    with c.SessionLocal() as db:
        org=db.get(c.Organization,payload.company_id)
        if not org or org.disposition!='active' or org.kind not in ('shipper','carrier') or org.id==user['org_id']:
            raise HTTPException(404,detail='Компания не найдена')
        official=db.scalar(select(c.User).where(c.User.org_id==org.id,c.User.active.is_(True),
            c.User.role==org.kind,c.User.is_admin.is_(True)).order_by(c.User.id).limit(1))
        if not official:c.invalid('У компании нет действующего администратора')
        if user['role']!='driver' and connected(db,org.id,user['org_id']):
            response.status_code=200
            return {'alreadyExists':True,'status':'accepted','message':'Компания уже в партнёрах'}
        if user['role']=='driver':
            kind='driver_join' if org.kind=='carrier' else 'driver_shipper'
        else:kind='peer' if org.kind==user['role'] else 'partner'
        invitation=c.InviteIn(kind=kind,role=org.kind,email=official.email)
    result=c.invite_participant(invitation,request,response,user)
    # The recipient already has an account and can decide inside their company cabinet.
    return {k:v for k,v in result.items() if k!='link'}


@router.get('/api/partners')
def journal(user=Depends(c.user_session)):
    data=c.list_participants(user)
    with c.SessionLocal() as db:
        role=user['role'];oid=user['org_id']
        parties=[]
        driver=db.scalar(select(c.Driver).where(c.Driver.user_id==user['id'])) if role=='driver' else None
        for item in data['partners']+data['companies']:
            org=db.get(c.Organization,item['id'])
            if org.disposition!='active':continue
            if role=='driver':
                endpoint=('/api/participants/carriers/' if item['role']=='carrier' else '/api/participants/shippers/')+str(item['id'])+'/leave'
            else:endpoint='/api/participants/organizations/'+str(item['id'])+'/remove'
            parties.append({**item,'inn':org.inn,'remove':endpoint,'shared':bool(role=='carrier' and db.get(c.FleetGrant,(oid,org.id))),'canViewFleet':bool(org.kind=='carrier' and (db.get(c.DriverFleetGrant,(org.id,driver.id)) if driver else db.get(c.FleetGrant,(org.id,oid))))})
        if role!='driver':
            for rel in db.scalars(select(c.CompanyPeer).where((c.CompanyPeer.first_id==oid)|(c.CompanyPeer.second_id==oid))):
                org=db.get(c.Organization,rel.second_id if rel.first_id==oid else rel.first_id)
                if org.disposition=='active':
                    sub='organization:'+str(org.id)
                    comment=db.scalar(select(c.ParticipantComment.body).where(c.ParticipantComment.user_id==user['id'],c.ParticipantComment.subject==sub))
                    parties.append(dict(id=org.id,name=org.name,inn=org.inn,role=org.kind,subject=sub,comment=comment or '',
                        remove='/api/partners/peer/'+str(org.id)+'/remove',canViewFleet=bool(org.kind=='carrier' and db.get(c.FleetGrant,(org.id,oid))),shared=bool(role=='carrier' and db.get(c.FleetGrant,(oid,org.id)))))
        for person in data['invitedDrivers']:
            parties.append({**person,'role':'driver','remove':'/api/participants/drivers/'+str(person['id'])+'/remove'})
        if role=='carrier':
            for person in data['roster']:
                parties.append({**person,'role':'driver','shared':bool(db.get(c.DriverFleetGrant,(oid,person['id']))),'remove':'/api/carrier/drivers/'+str(person['id'])+'/unlink'})
        invites=[]
        for inv in data['invitations']+data['archivedInvitations']:
            if inv['kind']=='team':continue
            row=db.get(c.ParticipantInvitation,inv['id'])
            target=db.get(c.User,row.target_user_id) if row.target_user_id else None
            name=inv['email']
            if inv['outgoing'] and target:
                name=target.name if target.role=='driver' else db.get(c.Organization,target.org_id).name
            elif not inv['outgoing']:
                sender=db.get(c.User,row.sender_user_id)
                name=sender.name if sender.role=='driver' else db.get(c.Organization,row.sender_org_id).name
            invites.append({**inv,'name':name})
        return {'partners':sorted(parties,key=lambda x:x['name'].casefold()),'invitations':invites}


@router.post('/api/partners/peer/{org_id}/remove')
def remove_peer(org_id:int,request:Request,user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf'])
    if user['role']=='driver':c.forbidden()
    with c.db_write() as db:
        ids=tuple(sorted((org_id,user['org_id'])))
        item=db.get(c.CompanyPeer,ids)
        if item:
            db.delete(item)
            for owner,other in (ids,ids[::-1]):
                db.execute(delete(c.FleetGrant).where(c.FleetGrant.carrier_id==owner,c.FleetGrant.partner_id==other))
    return {'ok':True}


class EmployeeIn(BaseModel):
    surname:str=Field(min_length=2,max_length=100)
    given_name:str=Field(default='',max_length=80)
    email:str=Field(min_length=6,max_length=180)
    password:str=Field(default='',max_length=128)


@router.get('/api/employees')
def employees(user=Depends(c.user_session)):
    if user['role'] not in ('shipper','carrier'):c.forbidden()
    with c.SessionLocal() as db:
        return [{'id':u.id,'name':u.name,'email':u.email,'active':u.active,'admin':u.is_admin}
                for u in db.scalars(select(c.User).where(c.User.org_id==user['org_id'],c.User.role==user['role']).order_by(c.User.name))]


@router.post('/api/employees',status_code=201)
def create_employee(payload:EmployeeIn,request:Request,user=Depends(c.user_session)):
    c.protect_csrf(request,user['csrf'])
    if not user['is_admin'] or user['role'] not in ('shipper','carrier'):c.forbidden()
    surname=c.clean_label(payload.surname)
    name=c.clean_label((surname+' '+payload.given_name.strip()).strip())
    if len(name)>100:c.invalid('Фамилия и имя вместе должны содержать до 100 символов',422)
    email=c.normalize_email(payload.email)
    raw=payload.password or secrets.token_urlsafe(20)
    if len(raw.strip())<12:c.invalid('Укажите пароль длиной от 12 символов',422)
    with c.db_write() as db:
        if db.scalar(select(c.User.id).where(c.User.email==email)):c.invalid('Эта почта уже зарегистрирована')
        u=c.User(org_id=user['org_id'],email=email,name=name,surname=surname,role=user['role'],
                 password_hash=c.pass_hash(raw),active=True,is_admin=False)
        db.add(u);db.flush()
        return {'id':u.id,'login':email,'initialPassword':raw if not payload.password else None}
