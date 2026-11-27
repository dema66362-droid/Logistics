"""Real forms + real server/database, never a mocked /api/state.

Uses disposable synthetic accounts in a new local DB per browser case. Does not
connect to Render, a user's browser profile, or a production database.
"""
from datetime import date, timedelta
import json
import os
import re
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
ARTIFACTS=ROOT/'artifacts'/'roles-2.9'
PASSWORD='QA-'+secrets.token_urlsafe(24)
HEADINGS={'shipper':'План отгрузок','carrier':'Кабинет перевозчика','driver':'Кабинет водителя'}

def field(page,form,name,value):
    page.locator(f'#{form} [name="{name}"]').fill(value)

def register(page,base,role,email,name,company):
    page.bring_to_front()
    page.goto(base,wait_until='domcontentloaded')
    page.get_by_role('button',name='Регистрация пользователя',exact=True).click()
    page.get_by_label('Роль',exact=True).select_option(role)
    if role!='driver':
        expect(page.get_by_label('Название организации',exact=True)).to_be_visible()
        page.get_by_label('Название организации',exact=True).fill(company)
        page.get_by_label('ИНН',exact=True).fill('0000000018')
    else: expect(page.get_by_label('Название организации',exact=True)).to_be_hidden()
    page.get_by_label('Ваше имя',exact=True).fill(name)
    page.get_by_label('Электронная почта',exact=True).last.fill('mailto:'+email)
    expect(page.get_by_label('Электронная почта',exact=True).last).to_have_value(email)
    page.get_by_label('Пароль (от 12 символов)',exact=True).fill(PASSWORD)
    page.get_by_role('button',name='Зарегистрироваться',exact=True).click()
    expect(page.get_by_role('dialog',name='Резервный код',exact=True)).to_be_visible()
    recovery_code=page.locator('#recoveryCode29').input_value()
    page.get_by_role('button',name='Я сохранил код',exact=True).click()
    expect(page.get_by_role('heading',name=HEADINGS[role],exact=True)).to_be_visible()
    expect(page.get_by_test_id('account-badge')).to_contain_text(email)
    return recovery_code

def participants(page):
    page.bring_to_front()
    page.get_by_role('button',name=re.compile('^(Партнёры|Список заказчиков)$'),exact=False).click()
    expect(page.get_by_role('dialog',name=re.compile('^(Партнёры|Список заказчиков)$'),exact=False)).to_be_visible()

def close(page):
    page.get_by_role('dialog').get_by_role('button',name='Закрыть',exact=True).click()
    expect(page.get_by_role('dialog')).to_have_count(0)

def invite_company(page,name):
    participants(page)
    page.get_by_role('button',name='Пригласить',exact=True).click()
    search=page.locator('#companySearch32')
    search.fill(name[:3])
    expect(page.locator('#found32 button')).to_have_count(0)
    search.fill(name)
    page.locator('#found32 .partner32').filter(has_text=name).get_by_role('button',name='Пригласить').click()
    expect(page.get_by_role('dialog',name=re.compile('^(Партнёры|Список заказчиков)$'),exact=False)).to_be_visible()
    close(page)

def accept(page):
    participants(page)
    button=page.get_by_role('button',name='Принять',exact=True)
    button.click()
    expect(button).to_have_count(0)
    close(page)

def signup_driver_invite(page,email):
    participants(page)
    page.get_by_role('button',name='Пригласить',exact=True).click()
    page.get_by_text('Пригласить водителя по почте',exact=True).click()
    page.get_by_label('Почта водителя').fill(email)
    with page.expect_response(lambda r:r.url.endswith('/api/participants/invitations')) as result:
        page.get_by_role('button',name='Пригласить водителя',exact=True).click()
    assert result.value.ok
    page.get_by_role('button',name='К партнёрам').click()
    close(page)

def check_public_collection(ship, carrier, driver, day):
    """A new customer can publish self-collection and approve an external crew from its bid."""
    ship.bring_to_front()
    ship.get_by_role('button', name='Новая заявка', exact=False).click()
    form = ship.locator('#newForm')
    delivery_fields = [form.locator('[name="deliveryAddress"]'), form.locator('[name="recipientContact"]')]
    mode = form.locator('[name="mode"]')
    for value, required in [('Самовывоз', False), ('Доставка', True), ('Самовывоз', False)]:
        mode.select_option(label=value)
        for control in delivery_fields:
            assert control.evaluate('el => el.required') is required, value

    for name, value in dict(to='Склад самовывоза', pickupAddress='Учебная площадка 3', date=day,
                            unload=day, recipient='Учебный получатель', terms='Самовывоз тары', budget='100000').items():
        field(ship, 'newForm', name, value)
    form.locator('[name="vehicleType"]').select_option(label='Фура')
    form.locator('[name="publicExternal"]').check()
    ship.get_by_role('button', name='Создать заявку', exact=True).click()
    expect(form).to_have_count(0)
    ship.get_by_role('button', name='Отправить заявку', exact=True).click()
    send = ship.get_by_role('dialog', name='Отправить заявку перевозчику')
    send.get_by_label('Искать по подходящим машинам', exact=False).check()
    send.get_by_role('button', name='Отправить заявку', exact=True).click()
    expect(send).to_have_count(0)

    carrier.bring_to_front()
    carrier.get_by_role('button', name='Обновить', exact=True).click()
    carrier.locator('.d31request').filter(has_text='Склад самовывоза').locator('[data-bid]').click()
    carrier.locator('#bidForm [name=driver]').select_option(label='Водитель QA')
    field(carrier, 'bidForm', 'price', '90000')
    carrier.locator('#bidForm [name="acceptTerms"]').check()
    carrier.get_by_role('button', name='Отправить заказчику', exact=True).click()
    expect(carrier.locator('#bidForm')).to_have_count(0)
    driver.bring_to_front()
    driver.get_by_role('button', name='Обновить', exact=True).click()
    expect(driver.locator('.d31request').filter(has_text='Склад самовывоза').locator('[data-driver-ok]')).to_have_count(0)
    ship.bring_to_front()
    ship.get_by_role('button', name='Обновить', exact=True).click()
    ship.locator('.d31queue').filter(has_text='Склад самовывоза').get_by_role('button', name='Предложения').click()
    details = ship.get_by_role('dialog', name='Карточка рейса')
    expect(details.get_by_role('button', name='Подтвердить перевозку')).to_have_count(0)
    with ship.expect_response(lambda r:r.url.endswith('/approve')) as sent:
        details.get_by_role('button', name='Пригласить перевозчика', exact=True).click()
    assert sent.value.ok
    close(ship)
    accept(carrier)
    ship.get_by_role('button',name='Обновить',exact=True).click()
    ship.locator('.d31queue').filter(has_text='Склад самовывоза').get_by_role('button',name='Предложения').click()
    details.get_by_role('button', name='Подтвердить перевозку', exact=True).click()
    ship.get_by_role('button', name='Подтвердить перевозку', exact=True).click()
    expect(ship.locator('[data-confirm]')).to_have_count(0)
    ship.locator('.d31queue').filter(has_text='Склад самовывоза').locator('[data-dispatch="detail"]').click()
    expect(ship.get_by_role('dialog')).to_contain_text('Перевозчик QA')
    expect(ship.get_by_role('dialog').locator('[data-complete]')).to_be_visible()
    close(ship)


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
            launch={}
            if engine_name=='chromium' and os.getenv('LOGISTIK_QA_CHROMIUM_EXECUTABLE'):
                launch['executable_path']=os.environ['LOGISTIK_QA_CHROMIUM_EXECUTABLE']
            browser=getattr(playwright,engine_name).launch(**launch)
            def new_page(key):
                ctx=browser.new_context(viewport=viewport,locale='ru-RU')
                contexts.append(ctx);page=ctx.new_page();page.set_default_timeout(12000)
                page.on('pageerror',lambda error:errors.append(str(error)))
                pages[key]=page;return page
            ship=new_page('shipper');carrier=new_page('carrier');driver=new_page('driver');carrier2=new_page('carrier2')
            recovery_codes={}
            recovery_codes['shipper']=register(ship,base,'shipper','shipper@example.test','Логист QA','Заказчик QA')
            recovery_codes['carrier']=register(carrier,base,'carrier','carrier@example.test','Перевозчик QA','Перевозчик QA')
            recovery_codes['driver']=register(driver,base,'driver','driver@example.test','Водитель QA','')
            register(carrier2,base,'carrier','carrier2@example.test','Перевозчик второй','Перевозчик второй')
            # Both invitation directions go through visible forms and explicit consent.
            invite_company(carrier,'Заказчик QA');accept(ship)
            invite_company(ship,'Перевозчик второй');accept(carrier2)
            participants(ship)
            expect(ship.locator('details summary').filter(has_text='Перевозчик QA')).to_be_visible()
            expect(ship.locator('details summary').filter(has_text='Перевозчик второй')).to_be_visible()
            close(ship)
            carrier.get_by_role('button',name='Транспорт',exact=True).click()
            fleet=carrier.locator('[data-dispatch-form="fleet"]')
            fleet.get_by_label('ФИО водителя').fill('Водитель QA')
            fleet.get_by_label('Госномер тягача').fill('А001АА77')
            fleet.get_by_label('Госномер прицепа').fill('АВ999977')
            fleet.get_by_label('Тип транспорта').select_option(label='Фура')
            fleet.get_by_label('Грузоподъёмность, т').fill('25')
            fleet.get_by_role('button',name='Сохранить экипаж').click()
            fleet.get_by_role('button',name='Подключить кабинет').click()
            contact=carrier.locator('[data-dispatch-form="rosterContact"]')
            contact.locator('[name=email]').fill('driver@example.test')
            contact.locator('button[type=submit]').click()
            expect(contact).to_have_count(0)
            # The company administrator creates a named login directly.
            ship.get_by_role('button',name='Сотрудники',exact=True).click()
            ship.get_by_text('Добавить сотрудника',exact=True).click()
            team=ship.locator('[data-form32="employee"]')
            team.locator('[name=surname]').fill('Смирнов')
            team.locator('[name=email]').fill('employee@example.test')
            team.locator('[name=password]').fill(PASSWORD)
            team.locator('button[type=submit]').click()
            ship.get_by_role('button',name='Готово').click()
            expect(ship.get_by_text('Смирнов',exact=True)).to_be_visible()
            close(ship)
            employee=new_page('employee');employee.goto(base)
            employee.get_by_label('Электронная почта',exact=True).fill('employee@example.test')
            employee.get_by_label('Пароль',exact=True).fill(PASSWORD)
            employee.get_by_role('button',name='Войти',exact=True).click()
            expect(employee.get_by_test_id('account-badge')).to_contain_text('Смирнов')
            employee.get_by_role('button',name='Сотрудники',exact=True).click()
            expect(employee.get_by_text('Добавить сотрудника',exact=True)).to_have_count(0)
            close(employee)
            signup_driver_invite(ship,'driver@example.test');accept(driver)
            signup_driver_invite(ship,'driver@example.test')
            participants(ship)
            row=ship.locator('details[data-partner-row]').filter(has_text='Водитель QA')
            expect(row).to_have_count(1);row.locator('summary').click()
            row.locator('textarea').fill('Личная заметка заказчика QA')
            row.get_by_role('button',name='Сохранить комментарий').click()
            expect(row.locator('[data-comment-status]')).to_have_text('Сохранено')
            close(ship);participants(ship)
            row.locator('summary').click();expect(row.locator('textarea')).to_have_value('Личная заметка заказчика QA')
            close(ship)
            # Full safe cargo dispatch from the newly registered accounts.
            ship.bring_to_front()
            ship.get_by_role('button',name='Новая заявка',exact=False).click()
            day=(date.today()+timedelta(days=10)).isoformat()
            for name,value in dict(to='Тестовый город Б',pickupAddress='Условная погрузка 1',deliveryAddress='Условная выгрузка 2',date=day,unload=day,recipient='Учебный склад',recipientContact='Тестовый контакт',terms='Учебные условия',budget='100000').items(): field(ship,'newForm',name,value)
            ship.locator('#newForm [name="vehicleType"]').select_option(label='Фура')
            ship.get_by_role('button',name='Создать заявку',exact=True).click()
            expect(ship.locator('#newForm')).to_have_count(0)
            ship.get_by_role('button',name='Отправить заявку',exact=True).click()
            send=ship.get_by_role('dialog',name='Отправить заявку перевозчику')
            send.get_by_label('Перевозчик QA',exact=True).check()
            send.get_by_role('button',name='Отправить заявку',exact=True).click()
            expect(send).to_have_count(0)
            carrier.bring_to_front()
            carrier.get_by_role('button',name='Обновить',exact=True).click()
            carrier.locator('[data-bid]').first.click()
            carrier.locator('#bidForm [name=driver]').select_option(label='Водитель QA')
            field(carrier,'bidForm','price','90000')
            carrier.locator('#bidForm [name="acceptTerms"]').check()
            carrier.get_by_role('button',name='Отправить заказчику',exact=True).click()
            expect(carrier.locator('#bidForm')).to_have_count(0)
            driver.bring_to_front()
            driver.get_by_role('button',name='Обновить',exact=True).click()
            month=driver.locator('[data-month-grid]')
            if day[:7]!=date.today().strftime('%Y-%m'):
                driver.locator('[data-dispatch="monthNext"]').click()
            expect(month).to_have_attribute('data-month-grid',day[:7])
            driver.locator('[data-dispatch="driverDay"][data-id="'+day+'"]').click()
            driver.locator('.d31job [data-dispatch="detail"]').first.click()
            driver.get_by_role('button',name='Предложить время',exact=True).click()
            driver.get_by_label('Время подачи',exact=True).fill('08:45')
            driver.get_by_role('button',name='Отправить заказчику',exact=True).click()
            expect(driver.get_by_role('dialog')).to_have_count(0)
            expect(driver.locator('[data-driver-ok]')).to_have_count(0)
            ship.bring_to_front()
            ship.get_by_role('button',name='Обновить',exact=True).click()
            dispute=ship.locator('.slotDispute').first
            expect(dispute).to_contain_text('09:00')
            expect(dispute).to_contain_text('08:45')
            dispute.get_by_role('button',name='Согласовать и поставить в очередь',exact=True).click()
            expect(ship.locator('.d31time').first).to_have_text('08:45')
            ship.get_by_role('button',name='Предложения',exact=True).last.click()
            ship.locator('[data-select-quote]').first.click()
            ship.get_by_role('button',name='Подтвердить перевозку',exact=True).click()
            expect(ship.locator('[data-confirm]')).to_have_count(0)
            driver.bring_to_front()
            driver.get_by_role('button',name='Обновить',exact=True).click()
            until=driver.locator('[data-month-grid]')
            expect(until).to_have_attribute('data-month-grid',day[:7])
            driver.locator('[data-dispatch="driverDay"][data-id="'+day+'"]').click()
            driver.locator('.d31job [data-dispatch="detail"]').first.click()
            expect(driver.get_by_role('dialog')).to_contain_text('Условная выгрузка 2')
            expect(driver.get_by_role('dialog')).to_contain_text('08:45')
            close(driver)
            driver.get_by_role('button',name='Отпуск',exact=True).click()
            driver.get_by_role('button',name='Внести отпуск',exact=True).click()
            trip_day=date.fromisoformat(day)
            leave_start=trip_day+timedelta(days=-3 if trip_day.day>3 else 1)
            leave=leave_start.isoformat()
            driver.get_by_label('Первый день',exact=True).fill(leave)
            driver.get_by_label('Последний день включительно',exact=True).fill((leave_start+timedelta(days=1)).isoformat())
            driver.get_by_role('button',name='Сохранить отпуск',exact=True).click()
            expect(driver.locator('[data-dispatch="driverDay"][data-id="'+leave+'"]')).to_contain_text('Отпуск')
            driver.locator('[data-dispatch="driverDay"][data-id="'+day+'"]').click()
            expect(driver.locator('.d31job').first).to_contain_text('08:45')
            driver.screenshot(path=str(ARTIFACTS/f'{case_name}-active-calendar.png'),full_page=True)
            # The driver can edit the winning trip; losing competitors' bids stay confidential.
            ds=driver.request.get(base+'/api/state').json()
            assert ds['orders'][0]['canEdit']
            driver.locator('.d31job [data-dispatch="detail"]').first.click()
            driver.get_by_role('button',name='Редактировать',exact=True).click()
            editor=driver.locator('[data-form32="edit"]')
            editor.locator('[name=budget]').fill('87000')
            editor.locator('button[type=submit]').click()
            expect(driver.get_by_role('dialog',name='Карточка рейса')).to_be_visible()
            expect(driver.get_by_role('dialog').locator('del.first32')).to_be_visible()
            close(driver)
            ship.bring_to_front()
            ship.locator('.d31queue [data-dispatch="detail"]').first.click()
            ship.locator('[data-complete]').first.click()
            expect(ship.locator('[data-complete]')).to_have_count(0)
            ship.locator('.d31queue [data-dispatch="detail"]').first.click()
            expect(ship.get_by_role('dialog')).to_contain_text('Завершена')
            close(ship)
            # All three actual account sessions have an active browser audio graph.
            for page in (ship,carrier,driver):
                print(json.dumps({'audio':page.evaluate('({visible:document.visibilityState,enabled:state.soundOn,engine:audioContext?.state,count:document.documentElement.dataset.soundCount,unread:state.notifications.filter(n=>!n.read).length})'),'case':case_name},ensure_ascii=False),flush=True)
                expect(page.locator('html')).to_have_attribute('data-sound-count',re.compile('[1-9][0-9]*'))
            external_shipper = new_page('external-shipper')
            register(external_shipper, base, 'shipper', 'external.ship@example.test', 'Новый логист', 'Новый заказчик')
            check_public_collection(external_shipper, carrier, driver, (trip_day+timedelta(days=8)).isoformat())
            # Removal changes the relevant pool, preserving the driver's login.
            participants(ship)
            ship.on('dialog',lambda dialog:dialog.accept())
            row=ship.locator('details[data-partner-row]').filter(has_text='Водитель QA')
            row.locator('summary').click();row.get_by_role('button',name='Удалить партнёра').click()
            expect(row).to_have_count(0)
            close(ship)
            for key in ('shipper','carrier','driver'):
                page=pages[key]
                page.bring_to_front()
                expect(page.get_by_test_id('account-badge')).to_contain_text(key+'@example.test')
                page.locator('[data-account]').click()
                expect(page.get_by_role('dialog',name='Учётная запись',exact=True)).to_contain_text(key+'@example.test')
                page.get_by_role('button',name='Послушать двигатель',exact=True).click()
                expect(page.locator('#soundStatus29')).to_contain_text('Сигнал запущен')
                close(page)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2'), key+' horizontal overflow'
                page.screenshot(path=str(ARTIFACTS/f'{case_name}-{key}.png'),full_page=True)
                page.get_by_role('button',name='Выход',exact=True).click()
                expect(page.get_by_role('heading',name='Вход в Логистик',exact=True)).to_be_visible()
                page.get_by_role('button',name='Забыли пароль?',exact=True).click()
                page.get_by_label('Почта учётной записи',exact=True).fill(key+'@example.test')
                page.get_by_label('Резервный код',exact=True).fill(recovery_codes[key])
                page.get_by_label('Новый пароль (от 12 символов)',exact=True).fill(PASSWORD+'New')
                page.get_by_label('Повторите новый пароль',exact=True).fill(PASSWORD+'New')
                page.get_by_role('button',name='Восстановить пароль',exact=True).click()
                expect(page.get_by_role('dialog',name='Резервный код',exact=True)).to_be_visible()
                page.get_by_role('button',name='Я сохранил код',exact=True).click()
                page.get_by_label('Электронная почта',exact=True).fill(key+'@example.test')
                page.get_by_label('Пароль',exact=True).fill(PASSWORD+'New')
                page.get_by_role('button',name='Войти',exact=True).click()
                expect(page.get_by_role('heading',name=HEADINGS[key],exact=True)).to_be_visible()
                expect(page.get_by_test_id('account-badge')).to_contain_text(key+'@example.test')
            assert not errors,errors
            return {'case':case_name,'engine':engine_name,'viewport':viewport,'passed':True,
                    'checks':['registration 3 roles with INN','driver monthly calendar and leave','driver counteroffer and shipper queue decision','shipper adds carrier','carrier adds shipper','carrier directly adds driver',
                              'employee personal login','vehicle creation','order-driver-selection-completion','driver edits shared price; original retained',
                              'customer invites driver','persistent single invitation','private participant comments',
                              'remove driver from pool','account identity each role','audio graph and loud test tone','password recovery 3 roles',
                              'self-collection without delivery details','bilateral external partnership and assignment','completed order summary matches actual status','logout','no JS errors','no horizontal overflow']}
        except Exception:
            for key,page in pages.items():
                try:
                    print(json.dumps({'diagnostic_role':key,'case':case_name,'audio':page.evaluate('({visible:document.visibilityState,enabled:state.soundOn,engine:audioContext?.state,count:document.documentElement.dataset.soundCount,error:document.documentElement.dataset.audioError})')},ensure_ascii=False),flush=True)
                    page.screenshot(path=str(ARTIFACTS/f'FAILED-{case_name}-{key}.png'),full_page=True)
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
    cases=[('firefox',{'width':1440,'height':1000},'firefox-desktop'),
           ('chromium',{'width':1440,'height':1000},'chromium-desktop'),
           ('chromium',{'width':390,'height':844},'chromium-mobile'),
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
