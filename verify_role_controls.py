"""Additional browser acceptance for the role journals and operator, using the case's private server."""
from datetime import date, timedelta
import re

from playwright.sync_api import expect


def more(page,label):
    page.locator('.headerMore summary').click()
    page.get_by_role('button',name=label,exact=True).click()


def admin_flow(page,base,initial,personal,artifacts,case):
    page.goto(base+'/admin')
    login=page.locator('#login')
    def sign(value):
        login.get_by_label('Логин',exact=True).fill('admin')
        login.get_by_label('Пароль',exact=True).fill(value)
        login.get_by_role('button',name='Войти',exact=True).click()
    sign('incorrect-in-test')
    expect(page.get_by_role('status')).to_contain_text('Неверный')
    sign(initial)
    setup=page.locator('#setup')
    expect(setup).to_be_visible()
    setup.get_by_role('button',name='Назад').click()
    sign(initial)
    setup.get_by_label('Новый пароль',exact=True).fill(personal)
    setup.get_by_label('Повторите пароль').fill(personal+'x')
    setup.get_by_role('button',name='Сохранить и войти').click()
    expect(page.get_by_role('status')).to_contain_text('не совпадают')
    setup.get_by_label('Повторите пароль').fill(personal)
    setup.get_by_role('button',name='Сохранить и войти').click()
    expect(page.locator('#identity')).to_contain_text('admin')
    inn=page.get_by_label('ИНН компании',exact=True)
    search=page.get_by_role('button',name='Найти компанию')
    inn.fill('0000000026');search.click()
    expect(page.get_by_role('status')).to_contain_text('Проверьте цифры')
    inn.fill('0000000032');search.click()
    expect(page.get_by_role('status')).to_contain_text('не найдена')
    inn.fill('0000000025');search.click()
    expect(page.locator('#result')).to_contain_text('Тест модерации')
    page.get_by_role('button',name='Очистить').click()
    expect(page.locator('#actions')).to_be_hidden()
    inn.fill('0000000025');search.click()
    block=page.get_by_role('button',name='Заблокировать',exact=True)
    def block_dialog(dialog):
        assert '0000000025' in dialog.message and 'Тест модерации' in dialog.message
        dialog.dismiss()
    page.once('dialog',block_dialog);block.click()
    expect(page.locator('#result')).to_contain_text('Доступ открыт')
    page.once('dialog',lambda d:d.accept());block.click()
    expect(page.locator('#result')).to_contain_text('Заблокирован')
    page.reload()
    expect(page.locator('#history li')).to_have_count(1)
    inn.fill('0000000025');search.click()
    page.once('dialog',lambda d:d.accept())
    page.get_by_role('button',name='Разблокировать',exact=True).click()
    expect(page.locator('#result')).to_contain_text('Доступ открыт')
    page.once('dialog',lambda d:d.dismiss())
    page.get_by_role('button',name='Удалить участника').click()
    expect(page.locator('#history li')).to_have_count(2)
    page.once('dialog',lambda d:d.accept())
    page.get_by_role('button',name='Удалить участника').click()
    expect(page.locator('#result')).to_contain_text('Удалён')
    expect(page.locator('#actions')).to_be_hidden()
    page.get_by_role('button',name='Обновить историю').click()
    expect(page.locator('#history li')).to_have_count(3)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+2')
    page.screenshot(path=str(artifacts/f'{case}-admin.png'),full_page=True)
    page.get_by_role('button',name='Выйти').click()
    expect(login).to_be_visible()
    sign(initial)
    expect(page.get_by_role('status')).to_contain_text('Неверный')
    sign(personal)
    expect(page.locator('#identity')).to_contain_text('admin')
    page.get_by_role('button',name='Выйти').click()
    page.get_by_role('link',name='Логистик',exact=True).click()
    expect(page.get_by_role('heading',name='Вход в Логистик')).to_be_visible()


def journal_controls(page,artifacts,case):
    page.get_by_role('button',name='Таблица',exact=True).click()
    table=page.get_by_role('table',name='Очередь рейсов')
    expect(table).to_be_visible()
    headers=table.locator('thead th').all_text_contents()
    assert headers==['Очередь','Подача','Маршрут и груз','Перевозчик и экипаж','Стоимость','Статус','Создал','Действие'],headers
    assert not re.search(r'NEW-[A-F0-9]+',table.inner_text())
    page.get_by_label('Поиск по неделе').fill('нет-такого-рейса')
    expect(page.get_by_text('По этому запросу рейсов нет.')).to_be_visible()
    page.get_by_label('Поиск по неделе').fill('тестовый город б')
    expect(table).to_be_visible()
    page.get_by_label('Поиск по неделе').fill('')
    route=table.locator('[data-dispatch=detail]').first
    route.click();expect(page.get_by_role('dialog')).to_contain_text('NEW-')
    page.keyboard.press('Escape');expect(page.get_by_role('dialog')).to_have_count(0)
    expect(table.locator('[data-dispatch=detail]').first).to_be_focused()
    page.screenshot(path=str(artifacts/f'{case}-journal.png'),full_page=True)
    expect(table.get_by_role('button',name='Отправить заявку')).to_be_visible()
    more_menu=page.locator('.headerMore summary')
    more_menu.click()
    with page.expect_download() as data:
        page.get_by_role('button',name='Скачать CSV').click()
    assert data.value.suggested_filename.endswith('.csv')


def share_and_plan(ship,carrier,driver,day,artifacts,case):
    # Grant and revoke visibility from opposite workspaces.
    carrier.get_by_role('button',name='Партнёры',exact=True).click()
    row=carrier.locator('details[data-partner-row]').filter(has_text='Заказчик QA')
    row.locator('summary').click()
    row.get_by_role('button',name='Открыть мой график').click()
    expect(row.locator('[data-b32=share]')).to_have_text('Закрыть мой график')
    carrier.get_by_role('dialog').get_by_role('button',name='Закрыть',exact=True).click()
    ship.get_by_role('button',name='Партнёры',exact=True).click()
    partner=ship.locator('details[data-partner-row]').filter(has_text='Перевозчик QA')
    partner.locator('summary').click();partner.get_by_role('button',name='График машин').click()
    shared=ship.get_by_role('dialog',name='График машин партнёра')
    expect(shared).to_be_visible();shared.get_by_role('button',name='Неделя →').click()
    shared.get_by_role('button',name='← Неделя').click()
    ship.keyboard.press('Escape')
    carrier.get_by_role('button',name='Параметры подбора').click()
    settings=carrier.get_by_role('dialog',name='Параметры подбора')
    settings.get_by_label('Порожний пробег, ₽/км').fill('31')
    settings.get_by_role('button',name='Сохранить расходы').click()
    expect(settings).to_contain_text('Ваши расходы')
    settings.get_by_text('Свободные машины и базы',exact=True).click()
    settings.get_by_label('Где свободна').fill('Тверь')
    settings.get_by_label('Город возврата').fill('Москва')
    with carrier.expect_response(lambda r:r.url.endswith('/api/fleet/position')) as result:
        settings.get_by_role('button',name='Сохранить позицию').click()
    assert result.value.ok
    settings.get_by_text('Пробег по вашим маршрутам',exact=True).click()
    for k,v in [('Из города','Тверь'),('В город','Москва'),('Дорожный пробег, км','180')]:
        settings.get_by_label(k,exact=True).fill(v)
    with carrier.expect_response(lambda r:r.url.endswith('/api/fleet/distance')) as result:
        settings.get_by_role('button',name='Сохранить пробег').click()
    assert result.value.ok
    expect(settings.get_by_text('Тверь → Москва · 180 км',exact=True)).to_have_count(1)
    carrier.keyboard.press('Escape')
    carrier.get_by_role('button',name='Запланировать рейс',exact=True).first.click()
    form=carrier.locator('[data-dispatch-form=manual]')
    trip=(date.fromisoformat(day)+timedelta(days=20)).isoformat()
    for k,v in {'customer':'Учебный клиент','product':'Коробки','tons':'5','day':trip,'unload':trip,
                'from':'Тверь','to':'Москва','sender':'Склад отправителя','pickupAddress':'Учебный адрес 1',
                'recipient':'Склад назначения','deliveryAddress':'Учебный адрес 2','deliveryContact':'Диспетчер',
                'terms':'Сухой закрытый кузов'}.items():
        form.locator('[name='+k+']').fill(v)
    form.get_by_role('button',name='Поставить в график').click()
    expect(form).to_have_count(0)
    job=carrier.locator('.d31job').filter(has_text='Учебный клиент')
    expect(job).to_contain_text('Тверь')
    carrier.screenshot(path=str(artifacts/f'{case}-fleet.png'),full_page=True)
    carrier.get_by_role('button',name='Все водители').click()
    carrier.locator('.d31board [data-dispatch=oneDriver]').first.click()
    job.locator('[data-dispatch=detail]').click()
    details=carrier.get_by_role('dialog',name='Карточка рейса')
    details.get_by_role('button',name='Завершить рейс',exact=True).click()
    expect(details).to_have_count(0)
    job.locator('[data-dispatch=detail]').click()
    expect(details).to_contain_text('Завершён')
    carrier.keyboard.press('Escape')
    carrier.get_by_role('button',name='Партнёры',exact=True).click()
    row.locator('summary').click()
    row.get_by_role('button',name='Закрыть мой график').click()
    expect(row.locator('[data-b32=share]')).to_have_text('Открыть мой график')
    carrier.keyboard.press('Escape')
