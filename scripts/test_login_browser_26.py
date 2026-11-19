"""Browser smoke for locked sign-in and self-service password form, no external browser accounts."""
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
html=(ROOT/'static/index.html').read_text('utf-8')
with sync_playwright() as p:
 b=p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
 page=b.new_page(viewport={'width':390,'height':844},device_scale_factor=1)
 errors=[]
 page.on('pageerror',lambda err:errors.append(str(err)))
 page.route('**/api/state',lambda route: route.fulfill(status=401,content_type='application/json',body='{"detail":"Необходим вход"}'))
 page.set_content(html,wait_until='load')
 page.wait_for_selector('form#loginForm')
 assert page.locator('input[name=email]').is_visible()
 assert page.locator('input[name=password]').is_visible()
 assert not page.locator('[data-role]').count()
 assert not errors,errors
 page.screenshot(path=str(ROOT/'LOGIN_2_6_PHONE.png'), full_page=True)
 # Stub the state API in a second browser page; password modal is a UI-only check.
 ctx2=b.new_context(viewport={'width':390,'height':844})
 page2=ctx2.new_page()
 errors2=[]
 page2.on('pageerror',lambda err:errors2.append(str(err)))
 mock='{"user":{"role":"driver","name":"Тест","email":"driver@test.invalid","organizationName":"Тест"},"csrf":"abc","orders":[],"offers":[],"drivers":[],"vehicles":[],"notifications":[],"day":"2026-11-19"}'
 inject='<script>window.EventSource=class {constructor(){}addEventListener(){}close(){}};window.fetch=async()=>new Response(JSON.stringify('+mock+'),{status:200,headers:{"Content-Type":"application/json"}})</script>'
 page2.set_content(html.replace('<script>',inject+'<script>',1),wait_until='load')
 page2.wait_for_selector('[data-password]')
 page2.locator('[data-password]').click()
 assert page2.locator('form#changePasswordForm').is_visible()
 assert page2.locator('input[name=current_password]').is_visible()
 assert page2.locator('input[name=new_password]').is_visible()
 assert page2.locator('input[name=confirm_password]').is_visible()
 assert not errors2,errors2
 ctx2.close()
 b.close()
print('LOGIN_AND_PASSWORD_MODAL_CHROMIUM_OK')
