'use strict';
// Keep header controls attached while asynchronous updates refresh trips and notification counts.
function renderDashboard31(markup) {
    const main=document.getElementById('main'),template=document.createElement('template');
    template.innerHTML=markup;
    const incoming=template.content.firstElementChild,current=main.firstElementChild;
    const identity=String(state.user.id)+'|'+state.view;
    if(!current?.matches('header.top')||!incoming?.matches('header.top')||current.dataset.identity!==identity){
        if(incoming)incoming.dataset.identity=identity;
        main.replaceChildren(template.content);return;
    }
    const previous=current.querySelectorAll('button'),next=incoming.querySelectorAll('button');
    if(previous.length!==next.length){incoming.dataset.identity=identity;main.replaceChildren(template.content);return}
    for(let i=0;i<previous.length;i++){
        const a=previous[i],b=next[i];
        for(const name of a.getAttributeNames())if(!b.hasAttribute(name))a.removeAttribute(name);
        for(const name of b.getAttributeNames())if(a.getAttribute(name)!==b.getAttribute(name))a.setAttribute(name,b.getAttribute(name));
        if(a.innerHTML!==b.innerHTML)a.innerHTML=b.innerHTML;
    }
    incoming.remove();
    while(current.nextSibling)current.nextSibling.remove();
    main.append(template.content);
}
const weekNames31=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
const stageNames31={waiting_driver:'В плане',driver_ok:'Экипаж в резерве',selected:'Назначен',completed:'Завершён'};
const shortDate31=d=>d?d.slice(8,10)+'.'+d.slice(5,7):'—';
const yearMonth31=d=>d.slice(0,7);
let dayFilter31='',fullColumns31=false,detailDriver31=0,form31={},planCache31={key:'',version:-1,data:null,request:0};
function button31(label,action,id='',primary=false){
    return `<button type="button" class="btn small ${primary?'primary':''}" data-dispatch="${esc(action)}" data-id="${esc(id)}">${esc(label)}</button>`;
}
function field31(label,name,options={}) {
    const {type='text',value='',required=true,extra=''}=options;
    return `<label class="field"><span>${esc(label)}</span><input type="${type}" name="${name}" value="${esc(value)}" ${required?'required':''} ${extra}></label>`;
}
function periodBar31(title,mode){
    return `<div class="section-head d31bar"><h2>${esc(title)}</h2><div class="d31actions">${button31('←',mode+'Prev')}${button31('Сегодня','today')}${button31('→',mode+'Next')}</div></div>`;
}
function slotStatus31(o,decisions=false) {
    if(!o.slotProposals?.length)return '';
    return `<section class="slotDispute" aria-label="Согласование времени"><b>Требуется согласование времени</b><small>В очереди · ${shortDate31(o.date)} в ${esc(o.time)}</small>${o.slotProposals.map(p=>`<div><span>${esc(roleTitle(p.role))} · ${esc(p.name)} · <b>${shortDate31(p.day)} в ${esc(p.time)}</b></span>${decisions?`<div class="d31actions">${button31('Согласовать и поставить в очередь','approveSlot',o.id+'/'+p.id,true)}${button31('Оставить прежнее время','rejectSlot',o.id+'/'+p.id)}</div>`:''}</div>`).join('')}</section>`;
}
function pendingDate(q){
    return !!q?.slotVersion && !(q.carrierAck && q.driverAck);
}
function orderStage31(o){
    if(['choice','confirmed'].includes(o.status)&&state.offers.some(q=>q.orderId===o.id&&['driver_ok','selected'].includes(q.status)&&pendingDate(q)))return 'Перенос на согласовании';
    if(o.internal)return stageNames31[o.privateStage]||'Закрыт';
    if(!['search','choice'].includes(o.status))return statuses[o.status]?.[0]||o.status;
    if(o.reviewOnly)return 'На проверке допуска';
    const q=state.offers.filter(q=>q.orderId===o.id);
    if(q.some(q=>q.status==='driver_ok'))return 'Выбор заказчика';
    if(q.some(q=>q.status==='waiting_driver'))return 'В плане';
    return o.searchLaunched?'У перевозчика':'Черновик';
}
function tableFields31(o) {
    return [['Заявка',o.id],['Создал',o.author],['Менеджер',o.manager],['Дата отгрузки',shortDate31(o.date)+' '+o.time],['Дата выгрузки',shortDate31(o.unload)],
        ['Грузополучатель',o.recipient],['Продукт',o.product],['Объём, т',o.tons],['№ электронной заявки',o.epd],['№ ЭТрН',o.etrn],
        ['Способ поставки',o.mode],['Способ доставки',o.vehicleType],['Перевозчик',carrierName32(o)],['Водитель',o.driver],['Адрес выгрузки',o.deliveryAddress],
        ['Данные автомобиля',o.car],['Данные на водителя',o.driver?'Карточка водителя':''],['Тариф за тонну',o.rate?rub(o.rate):''],['Стоимость перевозки',o.price?rub(o.price):'']];
}
function queueRows31(rows) {
    if(state.view==='table'&&rows.length)return choiceTable34(rows);
    if(fullColumns31 && rows.length){
        return `<div class="d31scroll"><table class="d31wide"><thead><tr><th>№ очереди</th>${tableFields31(rows[0]).map(([k])=>`<th>${esc(k)}</th>`).join('')}</tr></thead><tbody>${rows.map((o,i)=>`<tr><td>${i+1}</td>${tableFields31(o).map(([k,v],j)=>`<td>${j===0?button31(v,'detail',v):k==='Стоимость перевозки'?price32(o):esc(v||'—')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    }
    return rows.map((o,i)=>`<article class="d31queue"><div class="d31num">${i+1}</div><div class="d31time ${o.slotProposals?.length?'attention':''}">${esc(o.time)}</div><div class="d31route"><button class="d31link" data-dispatch="detail" data-id="${esc(o.id)}">${esc(o.from)} → ${esc(o.to)}</button><small>${esc(o.recipient)} · выгрузка ${shortDate31(o.unload)}</small></div><div class="d31crew">${esc(o.car||'Машина не назначена')}<small>Перевозчик · ${esc(carrierName32(o))}</small><small>${esc(o.driver||'')} ${price32(o)}</small><small>${esc(orderStage31(o))}</small></div><div class="d31actions">${(!o.searchLaunched&&!o.reviewOnly&&o.status==='search')?button31(o.hazardous?'Запросить проверку':'Отправить заявку','send',o.id,true):''}${o.status==='choice'?button31('Предложения','choose',o.id):''}</div>${slotStatus31(o,true)}</article>`).join('');
}
function shipperSchedule31(title){
    const dates=weekDates(),groups=dates.map(day=>{
        const rows=state.orders.filter(o=>o.date===day&&o.status!=='cancelled').sort((a,b)=>a.time.localeCompare(b.time));
        const selected=new Set(rows.filter(o=>o.selectedOffer).map(o=>o.selectedOffer));
        const cars=new Set(state.offers.filter(q=>selected.has(q.id)).map(q=>q.vehicleId));
        return {day,rows,cars:cars.size,open:rows.filter(o=>!o.selectedOffer).length};
    });
    const outside=state.orders.filter(o=>o.slotProposals?.length&&!dates.includes(o.date));
    const alerts=outside.length?`<section class="slotDispute"><b>На других датах требуется согласование · ${outside.length}</b>${outside.map(o=>button31(shortDate31(o.date)+' · '+o.from+' → '+o.to,'detail',o.id)).join('')}</section>`:'';
    const focus=groups.some(g=>g.day===dayFilter31)?dayFilter31:'';
    return htop(title,'Заказчик',`<button class="btn" data-export>CSV</button><button class="btn primary" data-new>＋ Новая заявка</button>`)+
        `<div class="d31">${alerts}<section class="panel d31panel">${periodBar31(weekTitle(),'week')}<div class="d31days">${groups.map((g,i)=>`<button class="d31day" data-dispatch="shipDay" data-id="${g.day}" aria-pressed="${focus===g.day}"><span>${weekNames31[i]}</span><b>${shortDate31(g.day)}</b><small>ТС · ${g.cars}</small></button>`).join('')}</div></section>
        <section class="panel d31panel"><div class="section-head"><div><h2>Очередь на погрузку</h2><small>По времени подачи</small></div><div class="d31actions">${focus?button31('Вся неделя','clearDay'):''}${state.view==='table'?'':button31(fullColumns31?'Кратко':'Все колонки','columns')}</div></div>${groups.filter(g=>!focus||g.day===focus).map(g=>`<div class="d31dayhead"><b>${weekNames31[dates.indexOf(g.day)]}, ${localeDay(g.day)}</b><span>Машин · ${g.cars} &nbsp; Без машины · ${g.open}</span></div>${queueRows31(g.rows)||'<p class="d31empty">Погрузок нет</p>'}`).join('')}</section></div>`;
}
overview=()=>shipperSchedule31('План отгрузок')+recommendPanel32();
table=()=>shipperSchedule31('Таблица отгрузок')+recommendPanel32();
allOffers=function(){
    const rows=state.orders.filter(o=>['search','choice'].includes(o.status)&&(o.slotProposals?.length||state.offers.some(q=>q.orderId===o.id&&['waiting_driver','driver_ok'].includes(q.status))));
    return htop('Предложения перевозчиков','Заказчик')+`<div class="d31"><section class="panel d31panel"><h2>Предложения по заявкам · ${rows.length}</h2>${rows.map(o=>`<div class="d31request"><div><button class="d31link" data-dispatch="detail" data-id="${esc(o.id)}">${esc(o.from)} → ${esc(o.to)}</button><small>${shortDate31(o.date)} в ${esc(o.time)} · ${esc(orderStage31(o))}</small></div>${button31('Открыть предложения','detail',o.id)}${slotStatus31(o,true)}</div>`).join('')||'<p class="d31empty">Предложений пока нет.</p>'}</section></div>`;
};

function dateOnJob31(j,d){return d>=j.offer.day&&d<=[j.offer.endDate,j.route?.stops.at(-1)?.eta.slice(0,10)||''].sort().at(-1)}
function scheduleRow31(j) {
    const o=j.order,q=j.offer;
    const stage=o.status==='completed'?'Завершён':pendingDate(q)?'Перенос на согласовании':stageNames31[q.status]||q.status;
    return `<div class="d31job"><div><button class="d31link" data-dispatch="detail" data-id="${esc(o.id)}">${esc(o.from)} → ${esc(o.to)}</button><small>${shortDate31(o.date)} ${esc(o.time)} · ${esc(o.pickupCompany||o.shipperName||'Погрузка')} → ${shortDate31(q.deliveryDay||o.unload)} · ${esc(o.recipient||'Выгрузка')}</small><small>Заказчик · ${esc(o.shipperName||'—')} &nbsp; Перевозчик · ${esc(q.carrier||o.carrier||'—')} &nbsp; ${esc(q.car||o.car||'')}</small></div><span class="pill ${j.confirmed?'green':'amber'}">${esc(stage)}</span></div>`;
}
function presentMonth31(data) {
    const row=data.drivers[0]||{jobs:[],leave:[]},dates=data.dates;
    const blank=(new Date(dates[0]+'T12:00:00').getDay()+6)%7;
    const chosen=dates.includes(state.day)?state.day:dates[0];
    const chosenJobs=row.jobs.filter(j=>dateOnJob31(j,chosen));
    const currentLeave=row.leave.find(x=>x.start<=chosen&&chosen<=x.end);
    const month=new Date(dates[0]+'T12:00:00').toLocaleDateString('ru-RU',{month:'long',year:'numeric'});
    return `<section class="panel d31panel" data-month-grid="${dates[0].slice(0,7)}">${periodBar31(month,'month')}<div class="d31month">${weekNames31.map(w=>`<span class="d31weekday">${w}</span>`).join('')}${'<span></span>'.repeat(blank)}${dates.map(d=>{
        const jobs=row.jobs.filter(j=>dateOnJob31(j,d)),leave=row.leave.some(x=>x.start<=d&&d<=x.end);
        const kind=leave?'vacation':jobs.some(j=>j.confirmed)?'booked':jobs.length?'pending':'free';
        const tag=leave?'Отпуск':jobs.length?('Рейс · '+jobs.length):'';
        return `<button type="button" class="d31monthday ${kind}" data-dispatch="driverDay" data-id="${d}" aria-label="${localeDay(d)} · ${tag||'Свободно'}" aria-pressed="${d===chosen}"><b>${Number(d.slice(8))}</b><small>${tag}</small></button>`;
    }).join('')}</div><div class="month-tracks34"><h3>Маршруты месяца</h3><div class="axis-scroll34"><div class="month-axis34" style="--days:${dates.length}"><div class="lane-axis34">${dates.map(d=>'<span>'+Number(d.slice(8))+'</span>').join('')}</div>${compactLane34(row,dates)}</div></div>${row.jobs.map(routeStrip34).join('')}</div><div class="d31legend"><span>● Назначен</span><span>● Экипаж в резерве</span><span>● Отпуск</span></div></section>
    <section class="panel d31panel"><div class="section-head"><h2>${localeDay(chosen)}</h2></div>${currentLeave?`<div class="d31job"><b>Отпуск · ${shortDate31(currentLeave.start)} — ${shortDate31(currentLeave.end)}</b></div>`:''}${chosenJobs.map(scheduleRow31).join('')||(!currentLeave?'<p class="d31empty">На этот день рейсов нет.</p>':'')}</section>`;
}
function carrierBoard31(data){
    const dates=data.dates;
    const rows=data.drivers.filter(r=>!detailDriver31||r.driver.id===detailDriver31).sort((a,b)=>a.driver.name.localeCompare(b.driver.name,'ru'));
    return `<section class="panel d31panel">${periodBar31(weekTitle(),'week')}<div class="section-head"><h2>График загруженности водителей</h2>${detailDriver31?button31('Все водители','allDrivers'):''}</div>
    <div class="fleet-scroll34"><div class="fleet-grid34"><div class="fleet-line34"><b>Водитель</b><div class="lane-axis34">${dates.map((d,i)=>'<span>'+weekNames31[i]+' '+shortDate31(d)+'</span>').join('')}</div></div>
    ${rows.map(row=>`<div class="fleet-line34"><button type="button" class="d31link" data-dispatch="oneDriver" data-id="${row.driver.id}">${esc(row.driver.name)}</button><div class="lane34">${compactLane34(row,dates)}</div></div>`).join('')}</div></div>
    ${!rows.length?'<p>В разделе «Транспорт» добавьте первого водителя.</p>':''}</section>
    ${detailDriver31&&rows[0]?`<section class="panel d31panel"><div class="section-head"><h2>${esc(rows[0].driver.name)}</h2>${button31('Запланировать рейс','manual',detailDriver31,true)}</div>${rows[0].jobs.map(routeStrip34).join('')||'<p>На эту неделю рейсов нет.</p>'}</section>`:''}`;
}
function planPanel31(){
    const month=state.user.role==='driver';
    const path=month?'/api/plan/month?month='+yearMonth31(state.day):'/api/plan?week='+weekStart(state.day);
    const key=[authGeneration,state.user.id,path].join('|');
    const cache=planCache31;
    if(key!==cache.key||cache.version!==lastStateRequest){
        if(cache.key!==key)cache.data=null;
        cache.key=key;cache.version=lastStateRequest;
        const seq=++cache.request,uid=state.user.id;
        api(path).then(data=>{
            if(seq!==cache.request||state.user?.id!==uid||cache.key!==key)return;
            cache.data=data;
            const section=document.querySelector('[data-period-plan]');
            if(section)section.innerHTML=month?presentMonth31(data):carrierBoard31(data);
        }).catch(err=>{
            if(seq!==cache.request||state.user?.id!==uid)return;
            const section=document.querySelector('[data-period-plan]');
            if(section)section.innerHTML=`<p role="alert">${esc(err.message)} ${button31('Повторить','refresh')}</p>`;
        });
    }
    return `<div data-period-plan aria-live="polite">${cache.data?(month?presentMonth31(cache.data):carrierBoard31(cache.data)):'<p class="d31empty">Загрузка графика…</p>'}</div>`;
}
driver=()=>htop('Кабинет водителя','Водитель',routeButton('Отпуск','leaveList',''))+`<div class="d31">${driverProfile()}${planPanel31()}</div>`;
carrier=function(){
    const inbox=state.orders.filter(o=>!o.internal&&['search','choice'].includes(o.status)&&!state.offers.some(q=>q.orderId===o.id&&['waiting_driver','driver_ok','selected'].includes(q.status)));
    const toolbar=button31('Транспорт','fleet')+' '+button31('Запланировать рейс','manual','',true);
    return htop('Кабинет перевозчика','Перевозчик',toolbar)+`<div class="d31"><section class="panel d31panel"><h2>Входящие заявки · ${inbox.length}</h2>${inbox.map(o=>`<article class="d31request"><div><button class="d31link" data-dispatch="detail" data-id="${esc(o.id)}">${esc(o.from)} → ${esc(o.to)}</button><small>${shortDate31(o.date)} ${esc(o.time)} · ${esc(o.shipperName||'Заказчик')} · ${esc(o.product)} · ${fmt(o.tons)} т</small></div><div class="d31actions">${o.reviewOnly?button31('Подтвердить получение','reviewSeen',o.id):`<button class="btn primary small" data-bid="${esc(o.id)}">Назначить экипаж</button>${button31('Предложить время','slot',o.id)}`}${o.addressed?button31('Отклонить','declineIncoming',o.id):''}</div>${slotStatus31(o)}</article>`).join('')||'<p class="d31empty">Новых заявок нет.</p>'}</section>${recommendPanel32()}${planPanel31()}</div>`;
};
function accountInn31(account) {
    if(state.user?.role==='driver')return '';
    if(!state.user?.is_admin)return `<p>ИНН · ${esc(account.inn||'Ещё не указан')}</p>`;
    return `<form data-dispatch-form="inn"><label class="field"><span>ИНН организации или ИП</span><input name="inn" required inputmode="numeric" pattern="([0-9]{10}|[0-9]{12})" maxlength="12" value="${esc(account.inn||'')}"></label><button class="btn small" type="submit">Сохранить ИНН</button><small role="alert"></small></form>`;
}
function participantRoster31(data) {
    if(state.user.role!=='carrier')return '';
    return `<details class="d31roster"><summary>Мои водители · ${data.roster?.length||0}</summary>${state.user.is_admin?button31('Добавить водителя','roster'):''}${(data.roster||[]).map(d=>`<div class="job"><b>${esc(d.name)}</b><small>${d.canReceive?'Кабинет подключён':'Личный кабинет не подключён'}</small><div class="d31actions">${state.user.is_admin&&!d.canReceive?button31('Указать почту','rosterContact',d.id):''}${state.user.is_admin?button31('Удалить из пула','unlink',d.id):''}</div>${participantNote29(d)}</div>`).join('')}</details>`;
}
function details31(ref){
    const o=getOrder(ref);
    if(!o)return '';
    const role=state.user.role,alive=['search','choice','confirmed'].includes(o.status),my=state.offers.filter(q=>q.orderId===ref);
    const proposed=slotStatus31(o,role==='shipper');
    const fields=[...(role==='shipper'?[]:[['Заказчик',o.shipperName]]),['Создал',o.author],['Подача по очереди',shortDate31(o.date)+' в '+o.time],['Город погрузки',o.from],['Грузоотправитель',o.pickupCompany],['Адрес погрузки',o.pickupAddress],['Контакт на погрузке',o.pickupContact],['Выгрузка',shortDate31(o.unload)],['Город выгрузки',o.to],['Грузополучатель',o.recipient],['Адрес выгрузки',o.deliveryAddress],['Контакт на выгрузке',o.recipientContact],['Продукт',o.product],['Объём, т',o.tons],['Автомобиль',o.car],['Водитель',o.driver],['Перевозчик',carrierName32(o)],['Требования',o.terms]];
    if(role!=='driver')fields.push(['Менеджер',o.manager],['Номер ЭПД',o.epd],['Номер ЭТрН',o.etrn],['Тариф за тонну',o.rate?rub(o.rate):''],['Комментарий',o.note]);
    let actions='';
    if(!o.internal&&alive&&!o.hazardous)actions+=button31(role==='shipper'?'Перенести дату или время':'Предложить время','slot',ref);
    if(role==='shipper'){
        if(o.status==='search'&&!o.searchLaunched&&!o.reviewOnly)actions+=button31(o.hazardous?'Запросить проверку':'Отправить заявку','send',ref,true);
        if(o.status==='confirmed')actions+=`<button class="btn small primary" data-complete="${esc(ref)}">Завершить рейс</button>`;
    } else if(o.internal){
        if(role==='carrier'&&o.privateStage==='selected')actions+=button31('Завершить рейс','internalComplete',ref,true);
    } else if(role==='carrier'){
        if(o.reviewOnly)actions+=button31('Подтвердить получение','reviewSeen',ref);
        else if(['search','choice'].includes(o.status)&&!my.some(q=>['waiting_driver','driver_ok','selected'].includes(q.status)))actions+=`<button class="btn small primary" data-bid="${esc(ref)}">Назначить экипаж</button>`;
    }
    if(o.canEdit && o.status!=='cancelled')actions+=action32('Редактировать','edit',ref)+action32('Отмена заявки','cancel',ref);
    const offers=my.filter(q=>['waiting_driver','driver_ok','selected'].includes(q.status)).sort((a,b)=>a.price-b.price||a.day.localeCompare(b.day)||a.pickup.localeCompare(b.pickup)).map(q=>{
        let buttons='';
        if(q.status==='driver_ok'&&role==='shipper'){
            if(!q.approved)buttons+=`<button class="btn small" data-approve-carrier="${esc(q.carrierId)}" title="Предложить партнёрство">Пригласить перевозчика</button>`;
            else if(!pendingDate(q)&&((q.day===o.date&&q.pickup===o.time&&!o.slotProposals?.some(p=>p.offerId===Number(q.id.slice(2))||p.carrierId===q.carrierId))||o.slotProposals?.some(p=>p.offerId===Number(q.id.slice(2))&&p.day===q.day&&p.time===q.pickup)))buttons+=`<button class="btn small primary" data-select-quote="${esc(q.id)}">Подтвердить перевозку</button>`;
            buttons+=`<button class="btn small" data-reject-quote="${esc(q.id)}">Отклонить предложение</button>`;
        }
        if(q.slotVersion){
            const mine=role==='carrier'?!q.carrierAck:role==='driver'?!q.driverAck:false;
            buttons+=`<div class="receipt-status">Перенос · перевозчик: ${q.carrierAck?'подтвердил':'ожидается ответ'} · водитель: ${q.driverAck?'подтвердил':q.driverConnected?'ожидается ответ':'кабинет не подключён'}</div>`;
            if(mine && alive)buttons+=button31('Подтвердить новую дату','confirmDate',q.id.slice(2)+'/'+q.slotVersion,true);
        }
        if(role==='carrier'&&q.status!=='selected')buttons+=`<button class="btn small" data-withdraw-quote="${esc(q.id)}">Отозвать предложение</button>`;
        return `<div class="job"><b>${esc(q.carrier)} · ${esc(q.driver)} · ${esc(q.car)}</b><small>${role==='driver'?'':rub(q.price)+' · '}Подача ${shortDate31(q.day)} в ${esc(q.pickup)} · выгрузка ${shortDate31(q.deliveryDay)} · ${esc(stageNames31[q.status])}</small><div class="d31actions">${buttons}</div></div>`;
    }).join('');
    const received=(o.recipients||[]).map(r=>esc(r.name)+' · '+({delivered:'В кабинете',review_delivered:'Запрос отправлен',review_seen:'Получение подтверждено',declined:'Отклонена'}[r.status]||'')).join('<br>');
    return dialog29('Карточка рейса',`<h3>${esc(o.from)} → ${esc(o.to)}</h3><p>${esc(o.id)} · ${esc(orderStage31(o))}</p>${proposed}${o.canEdit?'<p>Стоимость перевозки · '+price32(o)+'</p>':''}<div class="detail-grid">${fields.filter(([,v])=>v!==undefined&&v!==null&&v!=='').map(([k,v])=>`<div class="detail"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}</div>${received?'<h3>Получатели заявки</h3>'+received:''}${offers?'<h3>Экипаж и назначение</h3>'+offers:''}`,`<div class="d31actions">${actions}<button class="btn small" data-close>Закрыть</button></div>`);
}
detailsModal=details31;
function formDialog31(title,kind,body,submit){
    return dialog29(title,`<form data-dispatch-form="${kind}">${body}<p class="d31error" role="alert"></p><div class="modal-footer"><button class="btn" type="button" data-close>Отмена</button><button type="submit" class="btn primary">${esc(submit)}</button></div></form>`);
}
function openDispatch(kind,id=''){
    const o=getOrder(id);
    form31={kind,id,owner:state.user.id,ref:id,day:o?.date,time:o?.time,request:typeof crypto.randomUUID==='function'?crypto.randomUUID():Date.now()+'-'+Math.random().toString(36).slice(2)};
    state.modal='dispatch:'+kind;render();
}
function dispatchDialog() {
    const f=form31,o=getOrder(f.ref);
    if(f.owner!==state.user.id)return '';
    if(f.kind==='route')return routeDialog34();
    if(f.kind==='leaveList')return leaveList34();
    if(f.kind==='editLeave'){
        const l=f.leave;if(!l)return '';
        return formDialog31('Редактировать отпуск','editLeave',field31('Первый день','start',{type:'date',value:l.start})+
            field31('Последний день включительно','end',{type:'date',value:l.end}),'Сохранить отпуск');
    }
    if(f.kind==='fleet')return crewDirectory(f);
    if(f.kind==='leave')return formDialog31('Отпуск водителя','leave',field31('Первый день','start',{type:'date',value:state.day<state.businessDay?state.businessDay:state.day})+field31('Последний день включительно','end',{type:'date',value:state.day<state.businessDay?state.businessDay:state.day}),'Сохранить отпуск');
    if(f.kind==='roster')return formDialog31('Добавить своего водителя','roster',field31('Фамилия, имя','name',{extra:'maxlength="100"'})+field31('Почта кабинета водителя','email',{type:'email',required:false})+'<p class="strong-note">Водитель сразу появится в графике. Почту можно добавить позже. Для получения заявок водителю нужен личный кабинет.</p>','Добавить водителя');
    if(f.kind==='rosterContact')return formDialog31('Подключить кабинет водителя','rosterContact',field31('Почта личного кабинета','email',{type:'email'}),'Сохранить почту');
    if(f.kind==='slot'&&o){
        const mine=state.user.role==='driver'?state.offers.filter(q=>q.orderId===o.id&&['waiting_driver','driver_ok','selected'].includes(q.status)):[];
        const select=mine.length?'<label class="field">Перевозчик<select name="offer">'+mine.map(q=>`<option value="${esc(q.id.slice(2))}">${esc(q.carrier)} · ${esc(q.car)}</option>`).join('')+'</select></label>':'';
        return formDialog31(state.user.role==='shipper'?'Время подачи':'Предложить другое время','slot',select+`<p>В очереди · ${localeDay(f.day)} в ${esc(f.time)}</p>`+field31('Дата подачи','day',{type:'date',value:f.day})+field31('Время подачи','time',{type:'time',value:f.time}),state.user.role==='shipper'?'Сохранить перенос':'Отправить заказчику');
    }
    if(f.kind==='send'&&o){
        const options=(state.participants?.partners||[]).filter(p=>p.role==='carrier');
        const checks=options.map(p=>`<label class="d31check"><input type="checkbox" name="recipient" value="${p.id}" ${options.length===1?'checked':''}>${esc(p.name)}</label>`).join('');
        const broadcast=o.hazardous?'':`<label class="d31check"><input type="checkbox" name="search">Искать по подходящим машинам${o.publicExternal?', включая открытый поиск':''}</label>`;
        return formDialog31(o.hazardous?'Запросить проверку допуска':'Отправить заявку перевозчику','send',`<p>${esc(o.from)} → ${esc(o.to)} · ${shortDate31(o.date)} ${esc(o.time)}</p>${checks||'<p>В пуле пока нет перевозчиков. Подключите партнёра в разделе «Партнёры».</p>'}${broadcast}`,o.hazardous?'Отправить запрос проверки':'Отправить заявку');
    }
    if(f.kind==='manual'){
        const drivers=state.drivers.map(d=>`<option value="${d.id}" ${d.id===Number(f.id)?'selected':''}>${esc(d.name)}</option>`).join('');
        const cars=state.vehicles.map(v=>`<option value="${v.id}">${esc(v.label)} · ${esc(v.kind)} · ${fmt(v.capacity)} т</option>`).join('');
        if(!drivers||!cars)return dialog29('Запланировать рейс','<p>Для назначения добавьте водителя и машину в свой пул.</p>',button31('Добавить водителя','roster')+' <button class="btn" data-add-vehicle>Добавить машину</button>');
        return formDialog31('Запланировать рейс водителю','manual',`<div class="split"><label class="field">Водитель<select name="driver">${drivers}</select></label><label class="field">Машина<select name="vehicle">${cars}</select></label>${field31('Заказчик','customer')}${field31('Продукт','product')}${field31('Объём, т','tons',{type:'number',extra:'min="0.01" max="100" step="0.01"'})}${field31('Дата погрузки','day',{type:'date',value:state.day})}${field31('Время подачи','time',{type:'time',value:'09:00'})}${field31('Город погрузки','from')}${field31('Грузоотправитель','sender')}${field31('Адрес погрузки','pickupAddress')}${field31('Контакт на погрузке','pickupContact',{required:false})}${field31('Дата выгрузки','unload',{type:'date',value:state.day})}${field31('Город выгрузки','to')}${field31('Грузополучатель','recipient')}${field31('Адрес выгрузки','deliveryAddress')}${field31('Контакт на выгрузке','deliveryContact')}${field31('Условия перевозки','terms')}</div>`,'Поставить в график');
    }
    return '';
}
function moveMonth31(offset) {
    const first=state.day.slice(0,8)+'01';
    state.day=offset<0?plusDays(first,-1).slice(0,8)+'01':plusDays(first,32).slice(0,8)+'01';
}
document.addEventListener('click',async e=>{
    const btn=e.target.closest('[data-dispatch]');if(!btn||!state.user)return;
    e.preventDefault();e.stopImmediatePropagation();
    const action=btn.dataset.dispatch,id=btn.dataset.id||'',uid=state.user.id;
    try{
        if(action==='detail'||action==='choose'){state.modal='detail:'+id;render();return;}
        if(action==='send'){
            const p=await api('/api/participants');if(state.user?.id!==uid)return;
            state.participants=p;openDispatch(action,id);return;
        }
        if(['fleet','leave','slot','roster','rosterContact','manual'].includes(action)){openDispatch(action,id);return;}
        if(action==='columns'){fullColumns31=!fullColumns31;render();return;}
        if(action==='shipDay'||action==='clearDay'){dayFilter31=action==='clearDay'||dayFilter31===id?'':id;render();return;}
        if(action==='driverDay'){state.day=id;render();return;}
        if(action==='weekNext'||action==='weekPrev'){state.day=plusDays(state.day,action==='weekNext'?7:-7);dayFilter31='';render();return;}
        if(action==='monthNext'||action==='monthPrev'){moveMonth31(action==='monthNext'?1:-1);render();return;}
        if(action==='today'){state.day=state.businessDay;dayFilter31='';render();return;}
        if(action==='oneDriver'||action==='allDrivers'){detailDriver31=action==='allDrivers'?0:Number(id);render();return;}
        if(action==='refresh'){await loadState();return;}
        if(btn.disabled)return;btn.disabled=true;
        const path='/api/orders/'+encodeURIComponent(id);
        if(action==='confirmDate'){
            const [offer,version]=id.split('/');
            await api('/api/offers/'+Number(offer)+'/confirm-date','POST',{version:Number(version)});
        } else if(action==='approveSlot'||action==='rejectSlot'){
            const [ref,proposal]=id.split('/');
            const pending=getOrder(ref)?.slotProposals?.find(p=>p.id===Number(proposal));
            await api('/api/orders/'+encodeURIComponent(ref)+'/slot/'+Number(proposal),'POST',{accepted:action==='approveSlot'});
            if(pending&&action==='approveSlot'){state.day=pending.day;dayFilter31='';}
        } else if(action==='removeLeave'){
            if(!window.confirm('Убрать этот отпуск из графика?'))return;
            await api('/api/driver/leave/'+Number(id)+'/remove','POST');
            await loadState({fromEvent:true});await openLeave34();return;
        } else if(action==='reviewSeen')await api(path+'/acknowledge-review','POST');
        else if(action==='declineIncoming')await api(path+'/decline-incoming','POST');
        else if(action==='unlink'||action==='leaveCompany'){
            if(!window.confirm('Удалить из своего пула?'))return;
            await api(action==='unlink'?'/api/carrier/drivers/'+Number(id)+'/unlink':'/api/participants/carriers/'+Number(id)+'/leave','POST');
        } else if(action.startsWith('internal')){
            const verb=action.slice(8).toLowerCase();
            if(verb==='cancel'&&!window.confirm('Отменить этот рейс?'))return;
            await api('/api/carrier/plan/trips/'+encodeURIComponent(id)+'/'+verb,'POST');
        } else return;
        const changedRoster=action==='unlink'||action==='leaveCompany';
        await loadState({fromEvent:changedRoster});
        if(changedRoster)await refreshParticipants();
        toast(action==='approveSlot'?'Новое время внесено в очередь':action==='rejectSlot'?'В очереди сохранено прежнее время':'Сохранено');
    }catch(err){errorToast(err)}finally{if(btn.isConnected)btn.disabled=false}
},{capture:true});
document.addEventListener('submit',async e=>{
    const form=e.target.closest('[data-dispatch-form]');if(!form)return;
    e.preventDefault();e.stopImmediatePropagation();
    if(form.dataset.sending)return;
    form.dataset.sending='1';
    const submit=form.querySelector('[type=submit]');submit.disabled=true;
    const kind=form.dataset.dispatchForm,f=form31,values=new FormData(form),d=Object.fromEntries(values);
    let message='Сохранено';
    try{
        if(kind==='inn'){
            await api('/api/account/inn','POST',{inn:d.inn});account29=await api('/api/account');render();toast('ИНН сохранён');return;
        }
        if(f.owner!==state.user?.id)return;
        if(kind==='fleet'){
            await api('/api/carrier/crew','POST',{driver_id:Number(d.driver_id)||null,name:d.name||'',
                tractor:d.tractor,trailer:d.trailer,kind:d.kind,capacity:d.capacity,legacy_vehicle:Number(d.legacy)||null});
            await loadState({fromEvent:true});
            if(state.user?.id===f.owner){state.modal=f.ref?'bid:'+f.ref:'dispatch:fleet';render();toast('Экипаж сохранён')}
            return;
        }
        if(kind==='slot'){
            await api('/api/orders/'+encodeURIComponent(f.ref)+'/slot','POST',{day:d.day,time:d.time,expected_day:f.day,expected_time:f.time,offer_id:d.offer?Number(d.offer):null});
            if(state.user.role==='shipper'){state.day=d.day;dayFilter31=''}
            else message='Встречное время отправлено заказчику';
        } else if(kind==='roster')await api('/api/carrier/roster','POST',{name:d.name,email:d.email||''});
        else if(kind==='rosterContact')await api('/api/carrier/roster/'+Number(f.id)+'/contact','POST',{email:d.email});
        else if(kind==='editLeave'){await api('/api/driver/leave/'+Number(f.id),'PUT',{start:d.start,end:d.end,previous_start:f.leave.start,previous_end:f.leave.end});state.day=d.start;}
        else if(kind==='leave'){await api('/api/driver/leave','POST',{start:d.start,end:d.end});state.day=d.start;}
        else if(kind==='send'){
            const ids=values.getAll('recipient').map(Number),o=getOrder(f.ref);
            if(!ids.length&&!d.search)throw Error('Выберите перевозчика или поиск по машинам');
            if(ids.length&&d.search)throw Error('Выберите адресную отправку или поиск по машинам');
            const result=await api('/api/orders/'+encodeURIComponent(f.ref)+(o.hazardous?'/request-review':'/search'),'POST',ids.length?{carrier_ids:ids}:{public_external:!!o.publicExternal});
            message=result.notified===0?'Поиск запущен. Подходящих получателей пока нет.':'Заявка отправлена';
        } else if(kind==='manual'){
            const v=state.vehicles.find(v=>v.id===Number(d.vehicle));
            await api('/api/carrier/plan/trips','POST',{request_id:f.request,driver_id:Number(d.driver),vehicle_id:v.id,customer:d.customer,
                shipment:{from_city:d.from,to_city:d.to,product:d.product,tons:Number(d.tons),date:d.day,time:d.time,unload:d.unload,vehicle_type:v.kind,mode:'Доставка',
                    pickup_company:d.sender,pickup_address:d.pickupAddress,pickup_contact:d.pickupContact||'',recipient:d.recipient,delivery_address:d.deliveryAddress,recipient_contact:d.deliveryContact,terms:d.terms}});
            state.day=d.day;detailDriver31=Number(d.driver);
        }
        if(state.user?.id!==f.owner)return;
        if(form31===f&&state.modal==='dispatch:'+kind){
            // Read the committed period immediately, even when a notification is already in flight.
            planCache31.version=-1;
            closeModal();
        }
        await loadState({fromEvent:true});if(kind==='editLeave')await openLeave34();toast(message);
    }catch(err){const target=form.querySelector('[role=alert]');if(target)target.textContent=err.message;else errorToast(err)}
    finally{delete form.dataset.sending;if(submit.isConnected)submit.disabled=false;}
},{capture:true});
// A notification arriving during a foreground refresh must not supersede its form completion.
const readState31=loadState;
let activeStateRead31=null;
loadState=async function(options={}){
    const generation=authGeneration;
    if(options.fromEvent){
        if(activeStateRead31?.generation===generation){activeStateRead31.refreshAgain=true;return;}
        return readState31(options);
    }
    const ticket={generation,refreshAgain:activeStateRead31?.generation===generation && !!activeStateRead31.refreshAgain};
    activeStateRead31=ticket;
    try{return await readState31(options);}
    finally{
        if(activeStateRead31===ticket){
            activeStateRead31=null;
            if(ticket.refreshAgain && generation===authGeneration && state.user)loadState({fromEvent:true}).catch(()=>{});
        }
    }
};
const originalSignOut31=showLogin;
showLogin=function(){form31={};planCache31.request++;planCache31.key='';planCache31.data=null;detailDriver31=0;dayFilter31='';fullColumns31=false;originalSignOut31()};

function driverProfile(){
    const person=state.drivers[0];
    const rows=state.crews.map(c=>`<li><b>Тягач ${esc(c.tractor)} · Прицеп ${esc(c.trailer)}</b><small>${esc(c.company)}</small></li>`);
    return `<section class="panel d31panel"><h2>${esc(person?.name||state.user.name)}</h2>${rows.length?`<ul class="crew-list">${rows.join('')}</ul>`:'<p>Перевозчик ещё не закрепил тягач и прицеп.</p>'}</section>`;
}

function crewDirectory(form){
    const people=state.drivers.map(d=>`<option value="${d.id}">${esc(d.name)}</option>`).join('');
    const old=state.vehicles.filter(v=>!v.tractor&&!v.trailer);
    const rows=state.drivers.map(d=>{
        const crew=state.crews.find(c=>c.driverId===d.id);
        return `<li><b>${esc(d.name)}</b><small>${crew?'Тягач '+esc(crew.tractor)+' · Прицеп '+esc(crew.trailer):'Техника не закреплена'}${d.userId?'':' · Кабинет не подключён'}</small>${!d.userId?button31('Подключить кабинет','rosterContact',d.id):''}</li>`;
    });
    return formDialog31('Транспорт','fleet',
        (rows.length?`<ul class="crew-list">${rows.join('')}</ul>`:'')+
        `<label class="field">Карточка водителя<select name="driver_id"><option value="">Новый водитель</option>${people}</select></label>`+
        field31('ФИО водителя','name',{extra:'minlength="2" maxlength="100"'})+
        (old.length?`<label class="field">Машина из прежнего справочника<select name="legacy"><option value="">Новая или ранее зарегистрированная сцепка</option>${old.map(v=>`<option value="${v.id}">${esc(v.label)}</option>`).join('')}</select></label>`:'')+
        '<div class="split">'+field31('Госномер тягача','tractor',{extra:'maxlength="20"'})+
        field31('Госномер прицепа','trailer',{extra:'maxlength="20"'})+
        '<label class="field">Тип транспорта<select name="kind"><option>Автоцистерна</option><option>Фура</option></select></label>'+
        field31('Грузоподъёмность, т','capacity',{type:'number',extra:'min="0.01" max="120" step=".01"'})+'</div>',
        'Сохранить экипаж');
}

document.addEventListener('change',e=>{
    const input=e.target, form=input.form;
    if(form?.matches('[data-dispatch-form="fleet"]')){
        const f=form.elements;
        if(input.name==='driver_id'){
            const driver=state.drivers.find(d=>d.id===Number(input.value));
            const crew=state.crews.find(c=>c.driverId===driver?.id);
            f.name.value=driver?.name||'';f.name.readOnly=!!driver;
            f.tractor.value=crew?.tractor||'';f.trailer.value=crew?.trailer||'';
            f.kind.value=crew?.kind||'Автоцистерна';f.capacity.value=crew?.capacity||'';
            if(f.legacy)f.legacy.value='';
        }else if(input.name==='legacy'){
            const vehicle=state.vehicles.find(v=>v.id===Number(input.value));
            if(vehicle){f.kind.value=vehicle.kind;f.capacity.value=vehicle.capacity}
        }
    }
    if(form?.id==='bidForm' && input.name==='driver'){
        const crew=state.crews.find(c=>c.driverId===Number(input.value));
        const selector=form.elements.vehicle;
        selector.value=crew?String(crew.vehicleId):'';
        const msg=form.querySelector('[data-bid-crew]');
        msg.textContent=selector.selectedIndex>0?'Закреплённая техника подставлена.':
            'Выберите подходящую сцепку. Перевозчик может закрепить её в разделе «Мои водители и машины».';
    }
});
