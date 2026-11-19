"""Real forms + real server/database, never a mocked /api/state.

Uses disposable synthetic accounts in a new local DB per browser case. Does not
connect to Render, a user's browser profile, or a production database.
"""
from datetime import date, timedelta
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
ARTIFACTS=ROOT/'artifacts'/'roles-2.8'
PASSWORD='QA-'+secrets.token_urlsafe(24)
HEADINGS={'shipper':'План отгрузок','carrier':'Кабинет перевозчика','driver':'Кабинет водителя'}

def field(page,form,name,value):
    page.locator(f'#{form} [name="{name}"]').fill(value)

def register(page,base,role,email,name,company):
    page.goto(base,wait_until='domcontentloaded')
    page.get_by_role('button',name='Регистрация пользователя',exact=True).click()
    page.get_by_label('Роль',exact=True).select_option(role)
    if role!='driver':
        expect(page.get_by_label('Название организации',exact=True)).to_be_visible()
        page.get_by_label('Название организации',exact=True).fill(company)
    else: expect(page.get_by_label('Название организации',exact=True)).to_be_hidden()
    page.get_by_label('Ваше имя',exact=True).fill(name)
    page.get_by_label('Электронная почта',exact=True).last.fill('mailto:'+email)
    expect(page.get_by_label('Электронная почта',exact=True).last).to_have_value(email)
    page.get_by_label('Пароль (от 12 символов)',exact=True).fill(PASSWORD)
    page.get_by_role('button',name='Зарегистрироваться',exact=True).click()
    expect(page.get_by_role('heading',name=HEADINGS[role],exact=True)).to_be_visible()

def participants(page):
    page.get_by_role('button',name='Участники',exact=True).click()
    expect(page.get_by_role('dialog',name='Участники',exact=True)).to_be_visible()

def close(page):
    page.get_by_role('dialog').get_by_role('button',name='Закрыть',exact=True).click()
    expect(page.get_by_role('dialog')).to_have_count(0)

def invitation(page,kind_role,email):
    participants(page)
    page.get_by_label('Кого добавить',exact=True).select_option(kind_role)
    page.get_by_label('Электронная почта участника',exact=True).fill(email)
    page.get_by_role('button',name='Создать приглашение',exact=True).click()
    expect(page.locator('#copyInvitation')).to_be_visible()
    link=page.locator('#copyInvitation').input_value()
    close(page)
    return link

def accept(page):
    participants(page)
    page.get_by_role('button',name='Принять приглашение',exact=True).click()
    expect(page.get_by_role('dialog').get_by_text('Принято',exact=False).last).to_be_visible()
    close(page)

def run_case(playwright,engine_name,viewport,case_name):
    errors=[];pages={};contexts=[]
    with tempfile.TemporaryDirectory(prefix='logistik_roles_browser_') as tmp:
        env={k:v for k,v in os.environ.items() if not k.startswith('LOGISTIK_')}
        env.update(LOGISTIK_DATABASE_URL='sqlite:///'+str(Path(tmp)/'roles.sqlite3'),LOGISTIK_DEMO='0',
                   LOGISTIK_ALLOW_SIGNUP='1',LOGISTIK_DEPLOYMENT='pilot',LOGISTIK_SECURE_COOKIE='0')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        log=open(Path(tmp)/'server.log','w+')
        proc=subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
        browser=None
        try:
            for _ in range(100):
                try:
                    if httpx.get(base+'/health',timeout=.5,trust_env=False).status_code==200:break
                except (httpx.HTTPError,OSError):pass
                if proc.poll() is not None: raise RuntimeError('Test server stopped')
                time.sleep(.1)
            else: raise RuntimeError('Test server not ready')
            browser=getattr(playwright,engine_name).launch()
            def new_page(key):
                ctx=browser.new_context(viewport=viewport,locale='ru-RU')
                contexts.append(ctx);page=ctx.new_page();page.set_default_timeout(12000)
                page.on('pageerror',lambda error:errors.append(str(error)))
                pages[key]=page;return page
            ship=new_page('shipper');carrier=new_page('carrier');driver=new_page('driver');carrier2=new_page('carrier2')
            register(ship,base,'shipper','shipper@example.test','Логист QA','Заказчик QA')
            register(carrier,base,'carrier','carrier@example.test','Перевозчик QA','Перевозчик QA')
            register(driver,base,'driver','driver@example.test','Водитель QA','')
            register(carrier2,base,'carrier','carrier2@example.test','Перевозчик второй','Перевозчик второй')
            # Both invitation directions go through visible forms and explicit consent.
            invitation(carrier,'partner:shipper','shipper@example.test');accept(ship)
            invitation(ship,'partner:carrier','carrier2@example.test');accept(carrier2)
            participants(ship)
            expect(ship.get_by_role('dialog').get_by_text('Перевозчик QA',exact=True)).to_be_visible()
            expect(ship.get_by_role('dialog').get_by_text('Перевозчик второй',exact=True)).to_be_visible()
            close(ship)
            invitation(driver,'driver_join:carrier','carrier@example.test');accept(carrier)
            driver.goto(base,wait_until='domcontentloaded')
            driver.get_by_label('Электронная почта',exact=True).fill('driver@example.test')
            driver.get_by_label('Пароль',exact=True).fill(PASSWORD)
            driver.get_by_role('button',name='Войти',exact=True).click()
            expect(driver.get_by_role('heading',name=HEADINGS['driver'],exact=True)).to_be_visible()
            # A new employee creates their OWN password by the invitation link.
            link=invitation(ship,'team:shipper','employee@example.test')
            employee=new_page('employee');employee.goto(link,wait_until='domcontentloaded')
            expect(employee.get_by_role('dialog',name='Регистрация пользователя',exact=True)).to_be_visible()
            expect(employee.get_by_label('Название организации',exact=True)).to_be_hidden()
            employee.get_by_label('Ваше имя',exact=True).fill('Сотрудник QA')
            employee.get_by_label('Пароль (от 12 символов)',exact=True).fill(PASSWORD)
            employee.get_by_role('button',name='Зарегистрироваться',exact=True).click()
            expect(employee.get_by_role('heading',name=HEADINGS['shipper'],exact=True)).to_be_visible()
            participants(employee)
            expect(employee.get_by_label('Кого добавить')).to_have_count(1)
            expect(employee.locator('#participantType option[value="team:shipper"]')).to_have_count(0)
            close(employee)
            participants(carrier)
            carrier.get_by_role('button',name='+ Машина',exact=True).click()
            carrier.get_by_label('Обозначение машины',exact=True).fill('QA А001АА')
            carrier.get_by_label('Тип транспорта',exact=True).select_option(label='Фура')
            carrier.get_by_label('Грузоподъёмность, т',exact=True).fill('25')
            carrier.get_by_role('button',name='Добавить машину',exact=True).click()
            expect(carrier.get_by_role('dialog',name='Участники').get_by_text('QA А001АА',exact=True)).to_be_visible()
            close(carrier)
            # Full safe cargo dispatch from the newly registered accounts.
            ship.get_by_role('button',name='Новая заявка',exact=False).click()
            day=(date.today()+timedelta(days=10)).isoformat()
            for name,value in dict(to='Тестовый город Б',pickupAddress='Условная погрузка 1',deliveryAddress='Условная выгрузка 2',date=day,unload=day,recipient='Учебный склад',recipientContact='Тестовый контакт',terms='Учебные условия',budget='100000').items(): field(ship,'newForm',name,value)
            ship.locator('#newForm [name="vehicleType"]').select_option(label='Фура')
            ship.get_by_role('button',name='Создать заявку',exact=True).click()
            expect(ship.locator('#newForm')).to_have_count(0)
            ship.locator('[data-search-order]').first.click()
            expect(ship.locator('[data-search-order]')).to_have_count(0)
            carrier.get_by_role('button',name='Обновить',exact=True).click()
            carrier.locator('[data-bid]').first.click()
            field(carrier,'bidForm','price','90000')
            carrier.locator('#bidForm [name="acceptTerms"]').check()
            carrier.get_by_role('button',name='Отправить водителю',exact=True).click()
            expect(carrier.locator('#bidForm')).to_have_count(0)
            driver.get_by_role('button',name='Обновить',exact=True).click()
            driver.get_by_role('button',name='Подтверждаю',exact=False).click()
            expect(driver.locator('[data-driver-ok]')).to_have_count(0)
            ship.get_by_role('button',name='Обновить',exact=True).click()
            ship.locator('[data-select-quote]').first.click()
            ship.get_by_role('button',name='Назначить',exact=True).click()
            expect(ship.locator('[data-confirm]')).to_have_count(0)
            driver.get_by_role('button',name='Обновить',exact=True).click()
            expect(driver.get_by_text('Разгрузка: Условная выгрузка 2',exact=False)).to_be_visible()
            # Price is filtered by the real API, rather than hidden in the page.
            ds=driver.request.get(base+'/api/state').json()
            assert all(offer['price']==0 for offer in ds['offers'])
            ship.get_by_role('button',name='Показать все поля заявки',exact=True).click()
            ship.locator('[data-complete]').first.click()
            expect(ship.locator('[data-complete]')).to_have_count(0)
            for key in ('shipper','carrier','driver'):
                page=pages[key]
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2'), key+' horizontal overflow'
                page.screenshot(path=str(ARTIFACTS/f'{case_name}-{key}.png'),full_page=True)
                page.get_by_role('button',name='Выход',exact=True).click()
                expect(page.get_by_role('heading',name='Вход в Логистик',exact=True)).to_be_visible()
            assert not errors,errors
            return {'case':case_name,'engine':engine_name,'viewport':viewport,'passed':True,
                    'checks':['registration 3 roles','shipper adds carrier','carrier adds shipper','driver joins carrier',
                              'employee registers by link','vehicle creation','order-driver-selection-completion','driver price privacy','logout','no JS errors','no horizontal overflow']}
        except Exception:
            for key,page in pages.items():
                try: page.screenshot(path=str(ARTIFACTS/f'FAILED-{case_name}-{key}.png'),full_page=True)
                except Exception:pass
            log.flush();log.seek(0);print(log.read()[-9000:])
            raise
        finally:
            for ctx in contexts: ctx.close()
            if browser:browser.close()
            proc.terminate()
            try:proc.wait(timeout=6)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
            log.close()

def main():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    cases=[('chromium',{'width':1440,'height':1000},'chromium-desktop'),
           ('chromium',{'width':390,'height':844},'chromium-mobile'),
           ('firefox',{'width':1440,'height':1000},'firefox-desktop'),
           ('webkit',{'width':1440,'height':1000},'webkit-desktop')]
    selected=os.getenv('LOGISTIK_QA_BROWSER_CASE','')
    if selected:cases=[case for case in cases if case[2]==selected]
    assert cases,'Unknown browser case'
    results=[]
    with sync_playwright() as playwright:
        for engine,viewport,label in cases:
            result=run_case(playwright,engine,viewport,label);results.append(result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
    (ARTIFACTS/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
