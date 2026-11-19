"""Release regression: idempotent five-user demo fixture, browser-safe test data."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]


def test_render_blueprint_is_valid_shape_and_locked_down():
    spec=yaml.safe_load((ROOT/'render.yaml').read_text(encoding='utf-8'))
    assert len(spec['services']) == 1 and len(spec['databases']) == 1
    service=spec['services'][0]
    assert service['runtime']=='python' and service['type']=='web'
    assert service['plan']=='free'
    assert service['healthCheckPath']=='/health'
    assert service['autoDeployTrigger']=='off'
    env={v['key']:v for v in service['envVars']}
    for key,val in {'LOGISTIK_ALLOW_SIGNUP':'1','LOGISTIK_DEMO':'0','LOGISTIK_SECURE_COOKIE':'1','LOGISTIK_DEPLOYMENT':'pilot'}.items():
        assert str(env[key]['value']) == val
    for name in ('SHIPPER','CARRIER1','DRIVER1','CARRIER2','DRIVER2'):
        assert env['LOGISTIK_TEST_'+name+'_PASSWORD']['sync'] is False
    assert 'LOGISTIK_CLOUD_TEST_PASSWORD' not in env
    assert env['LOGISTIK_DATABASE_URL']['fromDatabase']['name']==spec['databases'][0]['name']
    assert spec['databases'][0]['ipAllowList']==[]


def test_fresh_pilot_seed_creates_one_trial_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        env={k:v for k,v in os.environ.items() if not k.startswith('LOGISTIK_')}
        env.update(LOGISTIK_DATABASE_URL='sqlite:///'+str(Path(tmp)/'check.sqlite3'),
                   LOGISTIK_DEPLOYMENT='pilot',LOGISTIK_DEMO='0',LOGISTIK_ALLOW_SIGNUP='0')
        script='''from pilot_data import create_pilot_data
from app import SessionLocal, User, Organization, Driver, Vehicle, Shipment, Approval
from sqlalchemy import select,func
assert create_pilot_data("Only-test-password-2372!") is True
assert create_pilot_data("Only-test-password-2372!") is False
with SessionLocal() as db:
 assert db.scalar(select(func.count()).select_from(User))==5
 assert db.scalar(select(func.count()).select_from(Organization))==3
 assert db.scalar(select(func.count()).select_from(Driver))==2
 assert db.scalar(select(func.count()).select_from(Vehicle))==2
 assert db.scalar(select(func.count()).select_from(Approval))==2
 assert db.scalar(select(func.count()).select_from(Shipment))==1
 s=db.scalar(select(Shipment))
 assert s.product=="Вода техническая" and not s.hazardous
 assert not s.search_launched and s.selected_offer_id is None
 assert s.from_city.startswith("Тестовый") and s.pickup_date >= __import__("datetime").date.today()
print("fresh-test-fixtures-ok")'''
        proc=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=env,
                            capture_output=True,text=True,timeout=30)
        assert proc.returncode==0,proc.stderr+proc.stdout
        assert 'fresh-test-fixtures-ok' in proc.stdout


def test_demo_and_original_design_are_unmodified():
    # Only authentication controls may change. The approved CSS must remain byte-for-byte identical.
    import hashlib
    from zipfile import ZipFile
    baseline = ROOT/'tests'/'approved_css_v2_5.css'
    html=(ROOT/'static/index.html').read_text(encoding='utf-8')
    assert html.split('<style>',1)[1].split('</style>',1)[0] == baseline.read_text(encoding='utf-8')
    assert 'id="loginForm"' in html and 'data-password' in html
    assert (ROOT/'DEMO_OFFLINE.html').stat().st_size > 40_000


def test_seeded_five_account_flow_through_http():
    """Users who log in after fresh deployment can finish the included test draft."""
    with tempfile.TemporaryDirectory() as tmp:
        env={k:v for k,v in os.environ.items() if not k.startswith('LOGISTIK_')}
        env.update(LOGISTIK_DATABASE_URL='sqlite:///'+str(Path(tmp)/'http.sqlite3'),
                   LOGISTIK_DEPLOYMENT='pilot', LOGISTIK_DEMO='0',LOGISTIK_ALLOW_SIGNUP='0')
        script='''from pilot_data import create_pilot_data
from app import app
from fastapi.testclient import TestClient
assert create_pilot_data("Only-test-password-2372!")
clients=[]
for email in ("pilot.shipper@example.test","pilot.carrier1@example.test",
              "pilot.driver1@example.test","pilot.carrier2@example.test","pilot.driver2@example.test"):
 c=TestClient(app); assert c.post("/api/login",json={"email":email,"password":"Only-test-password-2372!"}).status_code==200
 clients.append(c)
ship,c1,d1,c2,d2=clients
state=ship.get("/api/state").json();ref=state["orders"][0]["id"]
assert ref=="TEST-1001" and not state["orders"][0]["searchLaunched"]
assert not any(o["id"]==ref for o in c1.get("/api/state").json()["orders"])
def post(c,path,data=None):
 csrf=c.get("/api/state").json()["csrf"]
 return c.post(path,headers={"X-CSRF-Token":csrf},json=data)
assert post(ship,"/api/orders/TEST-1001/search",{"public_external":False}).status_code==200
assert all(ref in [o["id"] for o in c.get("/api/state").json()["orders"]] for c in (c1,c2))
from app import SessionLocal,Shipment,Offer
with SessionLocal() as db:
 s=db.query(Shipment).filter_by(ref=ref).one();start=s.pickup_date.isoformat();end=s.unload_date.isoformat()
q1=post(c1,"/api/offers",{"order_ref":ref,"driver_id":1,"vehicle_id":1,"day":start,"pickup":"09:30","end_date":end,"price":90000,"accept_terms":True})
assert q1.status_code==201,q1.text
id1=int(q1.json()["id"].removeprefix("Q-"))
assert post(d1,f"/api/offers/{id1}/driver-response?accepted=true").status_code==200
assert ship.get("/api/state").json()["orders"][0]["status"]=="confirmed"
assert d1.get("/api/state").json()["orders"][0]["deliveryAddress"].startswith("Условная")
assert d1.get("/api/state").json()["orders"][0]["budget"]==0
assert ref not in [o["id"] for o in c2.get("/api/state").json()["orders"]]
print("seeded-five-role-flow-ok")'''
        proc=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=env,
                            capture_output=True,text=True,timeout=45)
        assert proc.returncode==0,proc.stderr+proc.stdout
        assert 'seeded-five-role-flow-ok' in proc.stdout
