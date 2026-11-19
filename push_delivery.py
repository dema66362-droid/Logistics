"""Durable Web Push outbox; never exposes provider endpoints or keys to logs."""
from __future__ import annotations
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select


def private_key():
    return os.getenv('LOGISTIK_VAPID_PRIVATE_KEY','').strip()


def public_key():
    value=private_key()
    if not value:return ''
    try:
        raw=base64.urlsafe_b64decode(value+'='*(-len(value)%4))
        key=serialization.load_der_private_key(raw,password=None)
        if not isinstance(key,ec.EllipticCurvePrivateKey) or not isinstance(key.curve,ec.SECP256R1):return ''
        point=key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(point).decode().rstrip('=')
    except (ValueError,TypeError):return ''


def valid_endpoint(value):
    try:
        url=urlsplit(value);host=(url.hostname or '').lower()
        return (url.scheme=='https' and url.port in (None,443) and not url.username and not url.password
                and not url.fragment and len(value)<=2048 and
                (host=='fcm.googleapis.com' or host=='updates.push.services.mozilla.com'
                 or host.endswith('.push.services.mozilla.com') or host=='web.push.apple.com'
                 or host.endswith('.push.apple.com') or host.endswith('.notify.windows.com')))
    except ValueError:return False


def valid_keys(p256dh,auth):
    try:
        decode=lambda v:base64.urlsafe_b64decode(v+'='*(-len(v)%4))
        point=decode(p256dh);secret=decode(auth)
        if len(secret)!=16 or len(point)!=65:return False
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),point)
        return True
    except (ValueError,TypeError):return False


def send(subscription,payload):
    from pywebpush import webpush
    subject=os.getenv('LOGISTIK_PUBLIC_URL','https://'+os.getenv('RENDER_EXTERNAL_HOSTNAME','logistik-pilot-site.onrender.com')).rstrip('/')
    return webpush(subscription_info=subscription,data=json.dumps(payload,ensure_ascii=False),
        vapid_private_key=private_key(),vapid_claims={'sub':subject},ttl=3600,timeout=8)


def deliver_batch(core,send_function=None,limit=12):
    if not public_key():return 0
    send_function=send_function or send;done=0
    for _ in range(limit):
        now=datetime.now(timezone.utc)
        with core.db_write() as db:
            delivery=db.scalar(select(core.PushDelivery).where(core.PushDelivery.status.in_(['pending','retry','sending']),
                core.PushDelivery.next_attempt<=now).order_by(core.PushDelivery.id).limit(1))
            if not delivery:break
            note=db.get(core.Notification,delivery.notification_id);sub=db.get(core.PushSubscription,delivery.subscription_id)
            account=db.get(core.User,note.user_id) if note else None
            session=db.get(core.WebSession,sub.session_hash) if sub else None
            if (not sub or not note or not sub.active or note.read or not account or not account.active
                    or note.user_id!=sub.user_id or not session or session.user_id!=sub.user_id
                    or session.expires.replace(tzinfo=timezone.utc)<=now):
                delivery.status='discarded';continue
            delivery.status='sending';delivery.attempts+=1;delivery.next_attempt=now+timedelta(seconds=45)
            delivery_id=delivery.id;attempt=delivery.attempts
            subscription={'endpoint':sub.endpoint,'keys':{'p256dh':sub.p256dh,'auth':sub.auth}}
            # Lock-screen text carries no names, addresses, prices or credentials.
            payload={'title':'Логистик — новое уведомление','body':note.title,
                     'notificationId':note.id,'userId':note.user_id,'url':'/'}
        state='sent';disable=False
        try:send_function(subscription,payload)
        except Exception as exc:
            code=getattr(exc,'status_code',None) or getattr(getattr(exc,'response',None),'status_code',None)
            disable=code in (404,410)
            state='failed' if disable or code in (400,401,403) or attempt>=5 else 'retry'
        with core.db_write() as db:
            row=db.get(core.PushDelivery,delivery_id)
            if row and row.status=='sending':
                row.status=state
                row.next_attempt=datetime.now(timezone.utc)+timedelta(seconds=min(900,5*3**attempt))
                if disable:
                    current=db.get(core.PushSubscription,row.subscription_id)
                    if current:current.active=False
        done+=1
    return done


async def delivery_loop(core):
    while True:
        try:
            if public_key():await asyncio.to_thread(deliver_batch,core)
        except asyncio.CancelledError:raise
        except Exception:
            # Database/provider outages must not take down the web process.
            pass
        await asyncio.sleep(3)
