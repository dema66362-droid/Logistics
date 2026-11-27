"""Isolated website operator session. No public signup can create a site operator."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

import app as c

router=APIRouter()
COOKIE='logistik_site_operator'
COOKIE_PATH='/api/site-admin'


def password():
    value=os.environ.get('LOGISTIK_SITE_ADMIN_PASSWORD','')
    if len(value)<24:raise HTTPException(503,detail='Администратор сайта ещё не настроен владельцем')
    return value


def credential_tag(value):
    return hashlib.sha256(('site-operator:'+value).encode()).hexdigest()


def operator(request:Request):
    token=request.cookies.get(COOKIE,'')
    with c.SessionLocal() as db:
        row=db.get(c.AdminSession,c.sha_token(token)) if token else None
        if not row or row.expires.replace(tzinfo=timezone.utc)<=datetime.now(timezone.utc) or not hmac.compare_digest(row.credential_tag,credential_tag(password())):
            raise HTTPException(401,detail='Войдите как администратор сайта')
        return row.csrf


class Credentials(BaseModel):
    login:str=Field(min_length=1,max_length=100)
    password:str=Field(min_length=1,max_length=128)


@router.get('/admin',include_in_schema=False)
def admin_page():
    return FileResponse(c.BASE/'static'/'operator.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
        'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})


@router.post('/api/site-admin/login')
def login(form:Credentials,request:Request,response:Response):
    c.same_origin(request)
    c.consume_action('operator-login:'+(request.client.host if request.client else 'unknown'),10,5)
    secret=password()
    if not (hmac.compare_digest(form.login.encode(),os.getenv('LOGISTIK_SITE_ADMIN_LOGIN','admin').encode())
            and hmac.compare_digest(credential_tag(form.password),credential_tag(secret))):
        raise HTTPException(401,detail='Неверный логин или пароль')
    token=secrets.token_urlsafe(40);csrf=secrets.token_urlsafe(32);now=datetime.now(timezone.utc)
    with c.db_write() as db:
        db.execute(delete(c.AdminSession).where(c.AdminSession.expires<=now))
        db.add(c.AdminSession(token_hash=c.sha_token(token),csrf=csrf,expires=now+timedelta(hours=2),credential_tag=credential_tag(secret)))
    response.set_cookie(COOKIE,token,path=COOKIE_PATH,httponly=True,samesite='strict',secure=os.getenv('LOGISTIK_SECURE_COOKIE')=='1',max_age=7200)
    return {'csrf':csrf,'login':form.login}


@router.get('/api/site-admin/session')
def session(auth=Depends(operator)):
    return {'csrf':auth,'login':os.getenv('LOGISTIK_SITE_ADMIN_LOGIN','admin')}


@router.post('/api/site-admin/logout')
def logout(request:Request,response:Response,auth=Depends(operator)):
    c.protect_csrf(request,auth)
    with c.db_write() as db:db.execute(delete(c.AdminSession).where(c.AdminSession.token_hash==c.sha_token(request.cookies.get(COOKIE,''))))
    response.delete_cookie(COOKIE,path=COOKIE_PATH)
    return {'ok':True}


@router.get('/api/site-admin/company')
def lookup(inn:str,auth=Depends(operator)):
    inn=c.company_inn(inn)
    with c.SessionLocal() as db:
        rule=db.get(c.InnRestriction,inn)
        return {'inn':inn,'restriction':rule.state if rule else 'active',
                'companies':[{'name':o.name,'role':o.kind,'state':o.disposition}
                    for o in db.scalars(select(c.Organization).where(c.Organization.inn==inn).order_by(c.Organization.id))]}


class Moderation(BaseModel):
    inn:str=Field(max_length=12)
    action:Literal['block','unblock','delete']


@router.post('/api/site-admin/company')
def moderate(form:Moderation,request:Request,auth=Depends(operator)):
    c.protect_csrf(request,auth)
    inn=c.company_inn(form.inn)
    with c.db_write() as db:
        orgs=db.scalars(select(c.Organization).where(c.Organization.inn==inn)).all()
        if not orgs:raise HTTPException(404,detail='Компании с этим ИНН не зарегистрированы')
        ids=[o.id for o in orgs]
        rule=db.get(c.InnRestriction,inn)
        if rule and rule.state=='deleted' and form.action!='delete':c.invalid('Организация удалена')
        state={'block':'blocked','unblock':'active','delete':'deleted'}[form.action]
        if state=='active':
            if rule:db.delete(rule)
        else:
            if not rule:rule=c.InnRestriction(inn=inn);db.add(rule)
            rule.state=state;rule.changed_at=datetime.now(timezone.utc)
            people=select(c.User.id).where(c.User.org_id.in_(ids))
            db.execute(delete(c.WebSession).where(c.WebSession.user_id.in_(people)))
            db.execute(update(c.PushSubscription).where(c.PushSubscription.user_id.in_(people)).values(active=False))
            if state=='deleted':
                for approval in db.scalars(select(c.Approval).where((c.Approval.shipper_id.in_(ids))|(c.Approval.carrier_id.in_(ids)))):
                    approval.active=False
                db.execute(delete(c.FleetGrant).where((c.FleetGrant.carrier_id.in_(ids))|(c.FleetGrant.partner_id.in_(ids))))
                db.execute(delete(c.CompanyPeer).where((c.CompanyPeer.first_id.in_(ids))|(c.CompanyPeer.second_id.in_(ids))))
                db.execute(update(c.ParticipantInvitation).where(c.ParticipantInvitation.status=='pending',
                    (c.ParticipantInvitation.sender_org_id.in_(ids))|(c.ParticipantInvitation.target_user_id.in_(people))).values(status='cancelled'))
        for org in orgs:org.disposition=state
    return {'ok':True,'count':len(ids),'state':state}
