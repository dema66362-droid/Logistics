"""End-to-end trial over real HTTP sessions; replaces the server process mid-route."""
import os, sys, socket, subprocess, time, tempfile
from pathlib import Path
from datetime import date,timedelta
import httpx
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
 env={k:v for k,v in os.environ.items() if not k.startswith('LOGISTIK_')}
 env.update(LOGISTIK_DATABASE_URL='sqlite:///'+str(Path(tmp)/'qa.sqlite3'),LOGISTIK_ALLOW_SIGNUP='1',LOGISTIK_SECURE_COOKIE='0')
 with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def start():
  server=subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(port)],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  for i in range(95):
   try:
    if httpx.get(base+'/health',timeout=.4,trust_env=False).status_code==200:return server
   except httpx.HTTPError:pass
   time.sleep(.08)
  server.terminate();server.wait();raise RuntimeError('HTTP not ready')
 def stop(proc):proc.terminate();proc.wait(timeout=8)
 server=start()
 clients=[]
 try:
  for role in ['shipper','carrier','driver']:
   c=httpx.Client(base_url=base,trust_env=False,timeout=6);clients.append(c)
   r=c.post('/api/signup',json=dict(inn='0000000018',role=role,email=role+'@example.test',password='Local-QA-Only-Example-1',name='Учебный '+role,organization='Компания '+role));r.raise_for_status()
  ship,car,driver=clients
  def state(c):r=c.get('/api/state');r.raise_for_status();return r.json()
  def post(c,p,b=None):
   r=c.post(p,json=b,headers={'X-CSRF-Token':state(c)['csrf']});r.raise_for_status();return r.json()
  invitation=post(ship,'/api/participants/invitations',dict(email='carrier@example.test',role='carrier',kind='partner'))
  post(car,f"/api/participants/invitations/{invitation['id']}/respond",{'accepted':True})
  day=(date.fromisoformat(state(ship)['day'])+timedelta(days=10)).isoformat()
  payload=dict(from_city='Учебный город А',to_city='Учебный город Б',recipient='Грузополучатель Б',product='Вода техническая',
    tons=20,date=day,time='09:00',unload=day,vehicle_type='Фура',mode='Доставка',terms='Плановый рейс',budget=100000,
    pickup_company='Сторонний отправитель',pickup_address='Учебный адрес А',delivery_address='Учебный адрес Б',pickup_contact='Контакт А',recipient_contact='Контакт Б')
  ref=post(ship,'/api/orders',payload)['id']
  delivery=post(ship,'/api/orders/'+ref+'/search',dict(carrier_ids=[state(car)['user']['org_id']]))
  assert delivery['notified']==1 and state(car)['orders'][0]['id']==ref
  person=post(car,'/api/carrier/roster',dict(name='Учебный водитель',email='driver@example.test'))
  vehicle=post(car,'/api/carrier/vehicles',dict(label='Учебная фура',kind='Фура',capacity=24))
  qb=post(car,'/api/offers',dict(order_ref=ref,driver_id=person['id'],vehicle_id=vehicle['id'],day=day,pickup='11:15',end_date=day,price=90000,accept_terms=True))['id'][2:]
  def plan(c):r=c.get('/api/plan',params={'week':day});r.raise_for_status();return r.json()
  for c in [car,driver]:assert plan(c)['drivers'][0]['jobs'][0]['offer']['status']=='waiting_driver'
  stop(server);server=start()
  for c in [car,driver]:assert plan(c)['drivers'][0]['jobs'][0]['offer']['status']=='waiting_driver'
  post(driver,'/api/offers/'+qb+'/driver-response?accepted=true')
  proposal=state(ship)['orders'][0]['slotProposals'][0]
  post(ship,f'/api/orders/{ref}/slot/'+str(proposal['id']),{'accepted':True})
  post(ship,'/api/offers/'+qb+'/accept')
  for c in clients:
   o=state(c)['orders'][0];assert [o['time'],o['requestedTime'],o['pickupCompany'],o['recipient']]==['11:15','09:00','Сторонний отправитель','Грузополучатель Б']
  assert state(driver)['orders'][0]['pickupContact']=='Контакт А' and plan(car)['drivers'][0]['jobs'][0]['confirmed']
  post(ship,f'/api/orders/{ref}/complete')
  assert all(state(c)['orders'][0]['status']=='completed' for c in clients)
  print('PASS: 3 registered HTTP clients, addressed inbox without vehicle, direct roster, confirmation, server restart with persistent sessions and plans, synchronized 11:15, completion for all 3 accounts')
 finally:
  for c in clients:c.close()
  stop(server)
