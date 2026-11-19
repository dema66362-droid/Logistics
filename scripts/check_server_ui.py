"""Actual Chromium rendering of approved layout, with mocked API (no network access)."""
import json,os,tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
with tempfile.TemporaryDirectory() as tmp:
 os.environ['LOGISTIK_DATABASE_URL']='sqlite:///'+str(Path(tmp)/'db.sqlite3')
 os.environ['LOGISTIK_DEMO']='1'
 from app import app
 client=TestClient(app)
 states={}
 for role,email in [('shipper','shipper@example.test'),('carrier','carrier@example.test'),('driver','driver@example.test')]:
  t=TestClient(app)
  r=t.post('/api/login',json={'email':email,'password':'Demo-2026-Start!'})
  assert r.status_code==200,r.text
  states[role]=t.get('/api/state').json()
 html=(ROOT/'static/index.html').read_text(encoding='utf-8')
 with sync_playwright() as p:
  b=p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
  for role in states:
   ctx=b.new_context(viewport={'width':1440,'height':900})
   page=ctx.new_page();errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
   fake=json.dumps(states[role],ensure_ascii=False)
   js="""<script>const MOCK_DATA=XXX;window.EventSource=class{constructor(){} addEventListener(){} close(){}};window.fetch=async (url,opts)=>{if(url==='/api/state'){return new Response(JSON.stringify(MOCK_DATA),{status:200,headers:{'Content-Type':'application/json'}})}return new Response(JSON.stringify({detail:'Неразрешенное действие в макете'}),{status:403,headers:{'Content-Type':'application/json'}})}</script>""".replace('XXX',fake.replace('</','<\\/'))
   page.set_content(html.replace('<script>',js+'<script>',1),wait_until='load')
   page.wait_for_selector('.top h1')
   assert not errors,errors
   assert page.locator('button[data-view]:visible').count()==(3 if role=='shipper' else 1)
   assert page.locator('.side').evaluate('(el)=>getComputedStyle(el).backgroundColor')=='rgb(20, 33, 57)'
   path=ROOT/f'preview_{role}.png';page.screenshot(path=str(path),full_page=True)
   print(role,'rendered',page.locator('.top h1').inner_text(),path)
   if role=='shipper':
    page.get_by_role('button',name='Новая заявка').click()
    assert page.locator('form#newForm').is_visible()
    page.get_by_role('button',name='Отмена').click()
    page.locator('button[data-setview="table"]').click()
    assert page.locator('.big-table th').count()==19
   ctx.close()
  # mobile shipper
  ctx=b.new_context(viewport={'width':390,'height':844},device_scale_factor=1)
  page=ctx.new_page();js=js.replace(json.dumps(states['driver'],ensure_ascii=False).replace('</','<\\/'),json.dumps(states['shipper'],ensure_ascii=False).replace('</','<\\/'))
  # explicit override no dependencies on previous page
  fake=json.dumps(states['shipper'],ensure_ascii=False)
  js='<script>const MOCK_DATA='+fake.replace('</','<\\/')+';window.EventSource=class{constructor(){} addEventListener(){} close(){}};window.fetch=async()=>new Response(JSON.stringify(MOCK_DATA),{status:200,headers:{"Content-Type":"application/json"}})</script>'
  page.set_content(html.replace('<script>',js+'<script>',1),wait_until='load')
  page.wait_for_selector('.top h1')
  page.screenshot(path=str(ROOT/'preview_mobile.png'),full_page=True)
  assert page.locator('.side').evaluate('(el)=>getComputedStyle(el).position')=='fixed'
  ctx.close();b.close()
print('UI_CHROMIUM_OK: desktop shipper/carrier/driver + mobile + layout + modal + 19-column table')
