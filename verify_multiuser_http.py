"""End-to-end HTTP smoke with 5 independent sessions, synthetic safe cargo only.
Run from the release root: python verify_multiuser_http.py
Does not contact the Internet or change real deployment data.
"""
from __future__ import annotations
import os, sys, tempfile, socket, time, subprocess
from pathlib import Path
from datetime import date
import httpx

ROOT=Path(__file__).resolve().parent
ACCOUNTS=[
 ('pilot.shipper@example.test','shipper'),
 ('pilot.carrier1@example.test','carrier'),
 ('pilot.driver1@example.test','driver'),
 ('pilot.carrier2@example.test','carrier'),
 ('pilot.driver2@example.test','driver'),
]
PASSWORDS={email: 'LocalOnly-Unique-Test-'+str(i)+'-2026!' for i,(email,_) in enumerate(ACCOUNTS)}

def check(res, code, where):
    assert res.status_code==code, f'{where}: expected {code}, got {res.status_code}: {res.text[:300]}'
    return res.json()

def run():
    with tempfile.TemporaryDirectory(prefix='logistik_http_') as t:
        env={k:v for k,v in os.environ.items() if not k.startswith('LOGISTIK_')}
        env.update(LOGISTIK_DATABASE_URL='sqlite:///'+str(Path(t)/'smoke.sqlite3'),
                   LOGISTIK_DEPLOYMENT='pilot',LOGISTIK_DEMO='0',LOGISTIK_ALLOW_SIGNUP='0',
                   LOGISTIK_SECURE_COOKIE='0')
        seed='from pilot_data import create_pilot_data; import json,os; create_pilot_data(json.loads(os.environ["SMOKE_PASSWORDS"]))'
        seed_env={**env,'SMOKE_PASSWORDS':__import__('json').dumps(PASSWORDS)}
        subprocess.run([sys.executable,'-c',seed],cwd=ROOT,env=seed_env,check=True,capture_output=True)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        proc=subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(port),'--workers','1'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        clients=[]
        try:
            for _ in range(70):
                try:
                    if httpx.get(base+'/health',timeout=0.4,trust_env=False).status_code==200:break
                except (httpx.HTTPError, OSError): pass
                if proc.poll() is not None: raise RuntimeError('Server stopped during startup')
                time.sleep(.15)
            else: raise RuntimeError('Server did not start')
            outsider=httpx.Client(base_url=base,timeout=8,trust_env=False)
            check(outsider.get('/api/state'),401,'no login blocked')
            sessions=[]
            for email,role in ACCOUNTS:
                c=httpx.Client(base_url=base,timeout=8,trust_env=False)
                check(c.post('/api/login',json={'email':email,'password':PASSWORDS[email]}),200,email+' login')
                st=check(c.get('/api/state'),200,email+' state')
                assert st['user']['role']==role
                sessions.append({'client':c,'csrf':st['csrf'],'state':st})
                clients.append(c)
            ship,c1,d1,c2,d2=sessions
            assert [o['id'] for o in ship['state']['orders']]==['TEST-1001']
            for actor in [c1,c2,d1,d2]:
                assert actor['state']['orders']==[], 'Hidden orders leaked before publication'
            def post(actor,path,payload=None):
                return actor['client'].post(path,json=payload,headers={'X-CSRF-Token':actor['csrf']})
            response=post(ship,'/api/orders/TEST-1001/search',{'public_external':False})
            launched=check(response,200,'publish search')
            assert launched['notified']==2
            s1=check(c1['client'].get('/api/state'),200,'carrier 1 refresh')
            s2=check(c2['client'].get('/api/state'),200,'carrier 2 refresh')
            assert any(o['id']=='TEST-1001' for o in s1['orders'])
            assert any(o['id']=='TEST-1001' for o in s2['orders'])
            assert any(n['kind']=='new_order' for n in s1['notifications'])
            assert any(n['kind']=='new_order' for n in s2['notifications'])
            end=next(o for o in s1['orders'] if o['id']=='TEST-1001')['unload']
            start=next(o for o in s1['orders'] if o['id']=='TEST-1001')['date']
            def offer(actor,price):
                data=check(actor['client'].get('/api/state'),200,'carrier state')
                result=post(actor,'/api/offers',{
                    'order_ref':'TEST-1001','driver_id':data['drivers'][0]['id'],
                    'vehicle_id':data['vehicles'][0]['id'], 'day':start,'pickup':'11:00',
                    'end_date':end,'price':price,'accept_terms':True
                })
                return check(result,201,'offer submitted')['id']
            q1=offer(c1,95000)
            q2=offer(c2,92000)
            assert len(check(ship['client'].get('/api/state'),200,'shipper offers')['offers'])==2
            assert len(check(c1['client'].get('/api/state'),200,'carrier bid confidentiality')['offers'])==1
            assert len(check(c2['client'].get('/api/state'),200,'carrier bid confidentiality')['offers'])==1
            st_driver=check(d1['client'].get('/api/state'),200,'driver request')
            assert st_driver['offers'] and all(x['price']==0 for x in st_driver['offers'])
            assert st_driver['orders'][0].get('deliveryAddress','')==''
            assert st_driver['orders'][0].get('recipientContact','')==''
            check(post(d1,f'/api/offers/{int(q1.split("-")[-1])}/driver-response?accepted=true'),200,'driver 1 confirm')
            check(post(d2,f'/api/offers/{int(q2.split("-")[-1])}/driver-response?accepted=true'),200,'driver 2 confirm')
            check(post(ship,f'/api/offers/{int(q2.split("-")[-1])}/accept'),200,'shipper selects second carrier')
            final=check(ship['client'].get('/api/state'),200,'shipper final')
            assert next(o for o in final['orders'] if o['id']=='TEST-1001')['status']=='confirmed'
            assert check(d2['client'].get('/api/state'),200,'winning driver')['orders'][0]['deliveryAddress']
            assert check(d1['client'].get('/api/state'),200,'losing driver')['orders']==[]
            assert check(post(ship,f'/api/offers/{int(q1.split("-")[-1])}/accept'),409,'double assignment blocked')
            check(outsider.get('/api/export.csv'),401,'anonymous export blocked')
            print('PASS: 5 independent HTTP sessions, auth, privacy, two bids, driver confirmation, one winning assignment')
        finally:
            for c in clients: c.close()
            proc.terminate()
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=3)

if __name__=='__main__':run()
