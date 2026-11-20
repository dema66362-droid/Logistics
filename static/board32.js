'use strict';
const board32={owner:0,modal:'',data:null,id:'',token:0,week:'',search:0,selected:0};
const match32={key:'',data:null,seq:0};
const label32={pending:'Ожидает ответа',accepted:'Принято',rejected:'Отклонено',cancelled:'Отменено',expired:'Истекло'};
function action32(label,action,id='',primary=false){
    return `<button type="button" class="btn small ${primary?'primary':''}" data-b32="${esc(action)}" data-id="${esc(String(id))}">${esc(label)}</button>`;
}
function carrierName32(order){
    if(order.carrier)return order.carrier;
    const offers=state.offers.filter(x=>x.orderId===order.id&&['driver_ok','selected'].includes(x.status));
    if(offers.length)return [...new Set(offers.map(q=>q.carrier))].join(', ')+' · предложение';
    const addressed=(order.recipients||[]).filter(r=>r.status!=='declined').map(r=>r.name);
    return addressed.length?addressed.join(', ')+' · направлена заявка':'пока не назначен';
}
function price32(order){
    const current=order.currentPrice??order.price??order.budget;
    return `${order.priceChanged&&order.firstPrice!=null?`<del class="first32" title="Первоначальная стоимость">${esc(rub(order.firstPrice))}</del> `:''}<strong>${current?esc(rub(current)):'Цена не указана'}</strong>`;
}
function inp32(label,name,value='',type='text',extra=''){
    return `<label class="field"><span>${esc(label)}</span><input type="${type}" name="${name}" value="${esc(String(value??''))}" ${extra}></label>`;
}
function select32(label,name,items,value){
    return `<label class="field"><span>${esc(label)}</span><select name="${name}" required>${items.map(row=>`<option value="${esc(String(row[0]))}" ${String(row[0])===String(value)?'selected':''}>${esc(row[1])}</option>`).join('')}</select></label>`;
}
function form32(type,markup,submit){
    return `<form data-form32="${type}">${markup}<p class="d31error" role="alert"></p><button class="btn primary" type="submit">${esc(submit)}</button></form>`;
}
function controls32(content){return `<div class="d31actions">${content}</div>`}
function directory32(data){
    const rows=data.partners.map((p,i)=>{
        let buttons=action32('Удалить партнёра','remove',i);
        if(state.user.role==='carrier'&&(p.role!=='driver'||p.canReceive))buttons+=action32(p.shared?'Закрыть мой график':'Открыть мой график','share',i);
        if(p.canViewFleet)buttons+=action32('График машин','shared',p.id);
        if(state.user.role==='carrier'&&p.role==='driver'&&!p.canReceive)buttons+=button31('Подключить кабинет','rosterContact',p.id);
        return `<details class="partner32" data-partner-row="${Number(p.id)}"><summary>${esc(p.name)} <small>${esc(roleTitle(p.role||'driver'))}</small></summary>${p.inn?`<p>ИНН ${esc(p.inn)}</p>`:''}${controls32(buttons)}${participantNote29(p)}</details>`;
    }).join('');
    const shown=data.invitations.filter(x=>!x.archived);
    function inviteItem(x){
        let buttons='';
        if(x.status==='pending'&&!x.outgoing)buttons+=action32('Принять','accept',x.id,true)+action32('Отклонить','reject',x.id);
        if(x.status==='pending'&&x.outgoing&&x.canManage)buttons+=action32('Отозвать','withdrawInvite',x.id);
        if(x.outgoing&&x.canManage&&x.status!=='pending'&&!x.connected)buttons+=action32('Возобновить приглашение','renew',x.id);
        if(x.outgoing&&x.canManage&&x.status==='pending'&&x.kind==='shipper_driver')buttons+=action32('Получить ссылку','inviteLink',x.id);
        return `<div class="partner32"><b>${esc(x.name)}</b><small>${x.outgoing?'Исходящее':'Входящее'} · ${esc(label32[x.status]||x.status)}</small>${controls32(buttons)}</div>`;
    }
    return dialog29('Партнёры',controls32(action32('Пригласить','invite','',true)+(state.user.role==='carrier'?button31('Добавить водителя','roster'):''))
        +(rows||'<p class="d31empty">Пока нет партнёров.</p>')+`<h3>Приглашения</h3>${shown.filter(x=>x.status==='pending').map(inviteItem).join('')||'<p class="d31empty">Нет ожидающих приглашений.</p>'}
        <details class="partner32"><summary>История приглашений</summary>${data.invitations.filter(x=>x.status!=='pending'||x.archived).map(inviteItem).join('')||'История пуста.'}</details>`,
        controls32(action32('Обновить','partners')+'<button class="btn" data-close>Закрыть</button>'));
}
function invitation32(){
    const person=state.user.role==='shipper'?`<details class="partner32"><summary>Пригласить водителя по почте</summary>${form32('inviteDriver',inp32('Почта водителя','email','','email','required maxlength="180"'),'Пригласить водителя')}</details>`:'';
    return dialog29('Пригласить партнёра',`<label class="field">ИНН или название компании<input id="companySearch32" autocomplete="off" maxlength="160" placeholder="Введите минимум 4 символа"></label><div id="found32" aria-live="polite"></div>${person}<div id="personalLink32"></div>`,action32('К партнёрам','partners'));
}
function staff32(data){
    const items=data.map(u=>`<div class="partner32"><b>${esc(u.name)}</b><small>${esc(u.email)} · ${u.admin?'Администратор компании':u.active?'Сотрудник':'Доступ закрыт'}</small>${state.user.is_admin&&!u.admin&&u.active? action32('Закрыть вход','closeStaff',u.id):''}</div>`).join('');
    const create=state.user.is_admin?`<details class="partner32"><summary>Добавить сотрудника</summary>${form32('employee',inp32('Фамилия','surname','','text','required minlength="2" maxlength="100"')+inp32('Имя','given_name','','text','maxlength="80"')+inp32('Почта для входа','email','','email','required')+inp32('Начальный пароль · пустое поле для автоматического создания','password','','password','autocomplete="new-password" minlength="12" maxlength="128"'),'Создать личный кабинет')}</details>`:'';
    return dialog29('Сотрудники',create+items,'<button class="btn" data-close>Закрыть</button>');
}
function fleetSettings32(result){
    const co=result.costs;
    const costs=form32('costs',`<div class="split">${inp32('Порожний пробег, ₽/км','empty_km',co.empty_km,'number','required min=".01" step=".01"')}${inp32('С грузом, ₽/км','loaded_km',co.loaded_km,'number','required min=".01" step=".01"')}${inp32('День ожидания, ₽','wait_day',co.wait_day,'number','required min="0" step=".01"')}</div>`,'Сохранить расходы');
    const positions=result.positions.map(p=>`<p>${esc(state.vehicles.find(v=>v.id===p.vehicle)?.label||'Машина')} · ${esc(p.city)} с ${shortDate31(p.available)} · база ${esc(p.home)}</p>`).join('');
    const position=state.vehicles.length?form32('position',select32('Машина','vehicle_id',state.vehicles.map(v=>[v.id,v.label]),'')+inp32('Где свободна','city','','text','required minlength="2"')+inp32('Город возврата','home','','text','required minlength="2"')+inp32('Свободна с','available',state.businessDay,'date','required'),'Сохранить позицию'):'Сначала добавьте машину.';
    const dist=form32('distance',inp32('Из города','origin','','text','required')+inp32('В город','destination','','text','required')+inp32('Дорожный пробег, км','km','','number','min="0" step="1" required'),'Сохранить пробег');
    return dialog29('Параметры подбора',`<p>${result.custom?'Ваши расходы на перевозку.':'Начальные расходы ориентировочные. Укажите свои значения.'}</p>${costs}<details class="partner32"><summary>Свободные машины и базы</summary>${positions}${position}</details><details class="partner32"><summary>Пробег по вашим маршрутам</summary>${result.distances.map(p=>`<p>${esc(p.origin)} → ${esc(p.destination)} · ${Number(p.km)} км</p>`).join('')}${dist}</details>`);
}
function editor32(data){
    const o=data.order;
    const fields=[['Город погрузки','from_city',120],['Город выгрузки','to_city',120],['Грузоотправитель','pickup_company',160],['Грузополучатель','recipient',140],['Адрес погрузки','pickup_address',300],['Контакт на погрузке','pickup_contact',200],['Адрес выгрузки','delivery_address',300],['Контакт на выгрузке','recipient_contact',200],['Продукт','product',180],['Менеджер','manager',100],['№ ЭПД','epd',80],['№ ЭТрН','etrn',80]];
    let body=`<p>Заявка ${esc(board32.id)} · Создал ${esc(data.author||'—')}</p><div class="split">${fields.map(([label,k,n])=>inp32(label,k,o[k],'text',`maxlength="${n}" ${['from_city','to_city','recipient','product'].includes(k)?'required':''}`)).join('')}`;
    if(data.manual)body+=inp32('Заказчик рейса','customer',o.customer,'text','required');
    body+=inp32('Дата погрузки','date',o.date,'date','required')+inp32('Время подачи','time',o.time,'time','required')+inp32('Дата выгрузки','unload',o.unload,'date','required')+inp32('Объём, т','tons',o.tons,'number','min=".01" max="100" step=".01" required')+select32('Транспорт','vehicle_type',['Фура','Автоцистерна'].map(x=>[x,x]),o.vehicle_type)+select32('Поставка','mode',['Доставка','Самовывоз'].map(x=>[x,x]),o.mode)+'</div>';
    body+=`<p>Стоимость · ${price32(data)}</p><div class="split">${inp32('Стоимость рейса, ₽','budget',o.budget,'number','min="0" max="50000000" required')}${inp32('Тариф за тонну, ₽','tariff_per_ton',o.tariff_per_ton,'number','min="0" max="2000000"')}</div>`;
    body+=`<label class="field">Требования<textarea name="terms" maxlength="2000" required>${esc(o.terms||'')}</textarea></label><label class="field">Комментарий<textarea name="note" maxlength="2000">${esc(o.note||'')}</textarea></label>`;
    body+=`<label><input type="checkbox" name="hazardous" ${o.hazardous?'checked':''}> Требуется проверка допуска к перевозке</label>`;
    if(state.user.role==='shipper'&&!data.manual&&!getOrder(board32.id)?.searchLaunched)body+=`<label><input type="checkbox" name="public_external" ${o.public_external?'checked':''}> Доступна внешним перевозчикам</label>`;
    if(data.carriers.length){
        body+=select32('Перевозчик','carrier_id',data.carriers.map(x=>[x.id,x.name]),o.carrier_id)+`<div class="split" id="crew32">${crew32(o.carrier_id,o.driver_id,o.vehicle_id)}</div>`;
    }
    if(state.user.role!=='shipper'&&!data.manual)body+='<p class="strong-note">Изменение времени подачи поступит заказчику на согласование. В графике останется согласованное время.</p>';
    return dialog29('Редактировать заявку',form32('edit',body,'Сохранить изменения'));
}
function crew32(cid,driver,vehicle){
    const c=board32.data.carriers.find(x=>x.id===Number(cid));
    return c?select32('Водитель','driver_id',c.drivers.map(x=>[x.id,x.name]),driver)+select32('Машина','vehicle_id',c.vehicles.map(x=>[x.id,x.name]),vehicle):'';
}
function modal32(){
    if(board32.owner!==state.user?.id)return '';
    const {modal,data}=board32;
    if(modal==='partners')return directory32(data);
    if(modal==='invite')return invitation32();
    if(modal==='staff')return staff32(data);
    if(modal==='credential')return dialog29('Кабинет сотрудника создан',`<p>Логин · ${esc(data.login)}</p>${data.initialPassword?inp32('Начальный пароль · передайте лично сотруднику','initialPassword',data.initialPassword,'text','readonly autocomplete="off"'):'<p>Для входа действует указанный при создании пароль.</p>'}`,action32('Готово','staff'));
    if(modal==='settings')return fleetSettings32(data);
    if(modal==='edit')return editor32(data);
    if(modal==='trash')return dialog29('Заявки, помеченные на удаление',data.map(x=>`<div class="partner32"><b>${esc(x.ref)} · ${esc(x.route)}</b><small>Удаление · ${esc(new Date(x.at).toLocaleString('ru-RU'))}</small></div>`).join('')||'<p>Отменённых за последние сутки заявок нет.</p>');
    if(modal==='shared')return dialog29('График машин партнёра',controls32(action32('← Неделя','sharedPrev')+`<b>${shortDate31(board32.week)}</b>`+action32('Неделя →','sharedNext'))+(data.map(x=>`<div class="partner32"><b>${esc(x.vehicle)} · ${esc(x.from)} → ${esc(x.to)}</b><small>${shortDate31(x.start)} — ${shortDate31(x.end)}</small></div>`).join('')||'<p>На этой неделе назначенных рейсов нет.</p>'));
    return '';
}
async function open32(modal,id=''){
    const uid=state.user.id,seq=++board32.token,old=state.modal;
    const paths={partners:'/api/partners',staff:'/api/employees',edit:'/api/journal/'+encodeURIComponent(id)+'/editor',trash:'/api/journal-deletions',settings:'/api/fleet/settings',shared:'/api/fleet/shared/'+encodeURIComponent(id)+'?week='+encodeURIComponent(board32.week||state.day)};
    const data=paths[modal]?await api(paths[modal]):null;
    if(state.user?.id!==uid||seq!==board32.token||state.modal!==old)return;
    Object.assign(board32,{owner:uid,modal,id,data});state.modal='board:'+modal;render();
}
function renderMatches32(r){
    const carrier=state.user.role==='carrier';
    function row(x){
        const economics=carrier?(x.margin==null?'Расчёт неполный':`Остаток после расходов ${rub(x.margin)} · к возврату порожняком ${x.gain>=0?'+':''}${rub(x.gain)}`):(x.offeredPrice?`Цена перевозчика ${rub(x.offeredPrice)}`:'Цена предложения ещё не получена');
        return `<article class="partner32"><button class="d31link" data-dispatch="detail" data-id="${esc(x.order)}">${esc(x.fromCity)} → ${esc(x.toCity)}</button><b>${esc(x.vehicle)} · ${esc(x.carrier)}</b><small>${shortDate31(x.start)} ${esc(x.time)} · свободна в ${esc(x.availableCity)} · ${x.emptyKm==null?'пробег до подачи неизвестен':x.emptyKm+' км до подачи'}${x.needTime?' · уточнить время выгрузки':''}${!x.crewKnown?' · экипаж уточняется':''}</small><small>${esc(economics)}${carrier&&x.profitPercent!=null?' · '+Number(x.profitPercent)+'%':''}${!carrier&&x.mutual?' · выгоден обеим сторонам':''}</small><small>${esc(x.method)}${carrier&&!x.customCosts?' · ориентировочные расходы':''}</small></article>`;
    }
    const items=r.items;
    return items.slice(0,5).map(row).join('')+(items.length>5?`<details><summary>Остальные варианты · ${items.length-5}</summary>${items.slice(5).map(row).join('')}</details>`:'')||`<p class="d31empty">${carrier?'Нет подходящих открытых заявок на доступные машины. Для свободной машины можно указать город и дату в параметрах подбора.':'Для ваших заявок пока нет подходящих машин с открытым графиком.'}</p>`;
}
function recommendPanel32(){
    const carrier=state.user.role==='carrier',key=[authGeneration,state.user.id,lastStateRequest].join('/');
    if(match32.key!==key){
        const seq=++match32.seq;match32.key=key;const uid=state.user.id;
        if(match32.owner!==uid){match32.data=null;match32.owner=uid}
        api('/api/fleet/matches').then(r=>{if(seq!==match32.seq||uid!==state.user?.id)return;match32.data=r;const e=document.querySelector('[data-match-list]');if(e)e.innerHTML=renderMatches32(r)}).catch(e=>{if(seq===match32.seq){const el=document.querySelector('[data-match-list]');if(el)el.textContent=e.message}});
    }
    return `<section class="panel d31panel"><div class="section-head"><h2>${carrier?'Обратные загрузки':'Ближайшие машины партнёров'}</h2>${carrier?action32('Параметры подбора','settings'):''}</div><div data-match-list>${match32.data?renderMatches32(match32.data):'Подбираем варианты…'}</div></section>`;
}
let searchTimer32;
document.addEventListener('input',e=>{
    if(e.target.matches('[data-form32="edit"] [name="budget"],[data-form32="edit"] [name="tariff_per_ton"]'))e.target.form.dataset.basis=e.target.name==='budget'?'per_trip':'per_ton';
    if(e.target.id!=='companySearch32')return;
    const q=e.target.value.trim(),uid=state.user.id,seq=++board32.search,el=document.getElementById('found32');
    clearTimeout(searchTimer32);el.textContent=q.length<4?'':'Поиск…';
    if(q.length<4)return;
    searchTimer32=setTimeout(async()=>{
        try{const rows=await api('/api/partners/search?q='+encodeURIComponent(q));if(seq!==board32.search||uid!==state.user?.id||!el.isConnected)return;
            el.innerHTML=rows.map(x=>`<div class="partner32"><b>${esc(x.name)}</b><small>ИНН ${esc(x.inn||'не указан')} · ${esc(roleTitle(x.role))}</small>${action32('Пригласить','sendCompany',x.id)}</div>`).join('')||'Компаний не найдено.'}
        catch(err){if(el.isConnected&&seq===board32.search)el.textContent=err.message}
    },180);
});
document.addEventListener('change',e=>{
    if(e.target.matches('[data-form32="edit"] select[name="carrier_id"]'))document.getElementById('crew32').innerHTML=crew32(e.target.value,0,0);
});
function link32(value){
    const host=document.getElementById('personalLink32')||document.querySelector('#overlay .modal');
    if(!host||!value)return;
    host.querySelector('[data-invitation-copy]')?.remove();
    const field=document.createElement('label');field.className='field';field.dataset.invitationCopy='1';field.textContent='Персональная ссылка для водителя';
    const input=document.createElement('input');input.value=value;input.readOnly=true;field.append(input);host.append(field);input.select();
}
document.addEventListener('click',async e=>{
    const b=e.target.closest('[data-b32]');if(!b||!state.user)return;
    e.preventDefault();e.stopImmediatePropagation();
    const action=b.dataset.b32,id=b.dataset.id||'',uid=state.user.id;
    b.disabled=true;
    try{
        if(['partners','staff','invite','trash','settings','edit'].includes(action)){await open32(action,id);return}
        if(action==='shared'){board32.week=weekStart(state.day);await open32('shared',id);return}
        if(action==='sharedPrev'||action==='sharedNext'){board32.week=plusDays(board32.week,action==='sharedPrev'?-7:7);await open32('shared',board32.id);return}
        if(action==='cancel'){
            if(!confirm('Отменить заявку '+id+'? Участники получат уведомление, а через 24 часа заявка будет удалена.'))return;
            await api('/api/orders/'+encodeURIComponent(id)+'/cancel','POST');if(state.user?.id!==uid)return;closeModal();await loadState();toast('Заявка отменена. Срок удаления — через 24 часа.');return;
        }
        if(action==='closeStaff'){
            if(!confirm('Закрыть этому сотруднику вход в кабинет компании?'))return;
            await api('/api/participants/team/'+Number(id)+'/deactivate','POST');if(state.user?.id===uid)await open32('staff');return;
        }
        if(action==='inviteLink'){const r=await api('/api/participants/invitations/'+Number(id)+'/link','POST');if(state.user?.id===uid)link32(r.link);return}
        if(action==='sendCompany'){const r=await api('/api/partners/invite','POST',{company_id:Number(id)});if(state.user?.id===uid)toast(r.message||'Приглашение отправлено');}
        else if(action==='accept'||action==='reject'){await api('/api/participants/invitations/'+Number(id)+'/respond','POST',{accepted:action==='accept'});}
        else if(action==='renew')await api('/api/participants/invitations/'+Number(id)+'/renew','POST');
        else if(action==='withdrawInvite')await api('/api/participants/invitations/'+Number(id)+'/cancel','POST');
        else if(action==='share'||action==='remove'){
            const p=board32.data.partners[Number(id)];
            if(action==='share')await api('/api/fleet/share','POST',{partner_id:p.id,subject:p.role==='driver'?'driver':'company',enabled:!p.shared});
            else{if(!confirm('Удалить из партнёров '+p.name+'?'))return;await api(p.remove,'POST')}
        }else return;
        if(state.user?.id!==uid)return;await loadState({fromEvent:true});await open32('partners');
    }catch(err){errorToast(err)}
    finally{if(b.isConnected)b.disabled=false}
},true);
document.addEventListener('submit',async e=>{
    const f=e.target,kind=f.dataset.form32;if(!kind)return;
    e.preventDefault();e.stopImmediatePropagation();
    if(f.dataset.sending)return;
    f.dataset.sending='1';const btn=f.querySelector('[type=submit]');btn.disabled=true;
    const uid=state.user.id,body=Object.fromEntries(new FormData(f));const active=()=>state.user?.id===uid&&f.isConnected;
    try{
        if(kind==='edit'){
            const value={...board32.data.order,...body,price_basis:f.dataset.basis||'per_trip',hazardous:f.elements.hazardous.checked};
            if(f.elements.public_external)value.public_external=f.elements.public_external.checked;
            const r=await api('/api/journal/'+encodeURIComponent(board32.id),'PUT',value);if(!active())return;
            state.modal='detail:'+board32.id;await loadState({keepModal:true});toast(r.pendingSlot?'Изменения сохранены. Время отправлено заказчику на согласование.':'Изменения сохранены');return;
        }
        if(kind==='employee'){
            const r=await api('/api/employees','POST',body);if(!active())return;
            Object.assign(board32,{modal:'credential',data:r});state.modal='board:credential';render();return;
        }
        if(kind==='inviteDriver'){
            const r=await api('/api/participants/invitations','POST',{role:'driver',kind:'shipper_driver',email:body.email});
            if(active()){toast(r.message||'Приглашение сохранено');if(r.link)link32(r.link)}return;
        }
        if(['costs','position','distance'].includes(kind)){await api('/api/fleet/'+kind,'POST',body);if(active()){await loadState({fromEvent:true});await open32('settings');toast('Сохранено')}}
    }catch(err){if(active())f.querySelector('[role=alert]').textContent=err.message}
    finally{delete f.dataset.sending;if(btn.isConnected)btn.disabled=false}
},true);
const signout32=showLogin;
showLogin=function(){board32.token++;board32.search++;board32.owner=0;board32.data=null;match32.seq++;match32.key='';match32.data=null;signout32()};
// Dialogs are rendered in a shared overlay, including the signup and recovery screens.
const overlay=document.getElementById('overlay');
let openDialogTitle='',returnTarget=null,returnSelector='';
function rememberControl(element){
    if(!element||overlay.contains(element))return;
    returnTarget=element;
    const data=element.getAttributeNames().filter(name=>name.startsWith('data-')&&name!=='data-testid');
    returnSelector=data.length?element.tagName+data.map(name=>'['+name+'="'+CSS.escape(element.getAttribute(name))+'"]').join(''):'';
}
window.addEventListener('click',event=>{
    const control=event.target.closest('button,a,summary');
    if(control&&!overlay.querySelector('[role=dialog]'))rememberControl(control);
    const menu=document.querySelector('.headerMore[open]');
    if(menu&&(!menu.contains(event.target)||event.target.closest('button')))menu.open=false;
},true);
function focusableIn(dialog){
    return [...dialog.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,a[href]')]
        .filter(el=>el.getClientRects().length);
}
new MutationObserver(()=>{
    const dialog=overlay.querySelector('[role=dialog]');
    if(!dialog){
        if(openDialogTitle){
            const target=returnTarget?.isConnected?returnTarget:returnSelector?document.querySelector(returnSelector):null;
            (target||document.querySelector('[data-account]')||document.getElementById('loginEmail'))?.focus({preventScroll:true});
        }
        openDialogTitle='';return;
    }
    for(const group of dialog.querySelectorAll('.field')){
        const label=group.querySelector(':scope > label'),input=group.querySelector(':scope > input,:scope > select,:scope > textarea');
        if(label&&input&&!label.htmlFor&&input.name){if(!input.id)input.id='field-'+input.closest('form')?.id+'-'+input.name;label.htmlFor=input.id}
    }
    const title=dialog.getAttribute('aria-label')||state.modal;
    if(title!==openDialogTitle||!dialog.contains(document.activeElement)){
        openDialogTitle=title;
        const input=dialog.querySelector('input:not([type=hidden]):not([readonly]),select,textarea');
        const target=input?.getClientRects().length?input:dialog.querySelector('h3')||dialog;
        if(!target.matches('input,select,textarea'))target.tabIndex=-1;
        target.focus({preventScroll:true});
    }
}).observe(overlay,{childList:true});
document.addEventListener('keydown',event=>{
    const dialog=overlay.querySelector('[role=dialog]');
    if(!dialog){
        const menu=document.querySelector('.headerMore[open]');
        if(menu&&event.key==='Escape'){menu.open=false;menu.querySelector('summary').focus()}
        return;
    }
    if(event.key==='Escape'&&dialog.querySelector('[data-close]')){event.preventDefault();closeModal();return}
    if(event.key!=='Tab')return;
    const items=focusableIn(dialog);
    if(!items.length)return;
    const first=items[0],last=items.at(-1);
    if(event.shiftKey&&(document.activeElement===first||!items.includes(document.activeElement))){event.preventDefault();last.focus()}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
});
startApp();
