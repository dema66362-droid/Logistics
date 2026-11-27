'use strict';
const time34=value=>value?shortDate31(value.slice(0,10))+' '+value.slice(11,16):'—';
const km34=km=>km==null?'пробег не определён':'≈ '+fmt(km)+' км';
function elapsed34(a,b){
    const minutes=Math.round((Date.parse(b+'Z')-Date.parse(a+'Z'))/60000);
    if(minutes<0)return 'пересечение графика';
    const days=Math.floor(minutes/1440),hours=Math.floor(minutes%1440/60),rest=minutes%60;
    return [days?days+' дн.':'',hours?hours+' ч':'',!days&&rest?rest+' мин':''].filter(Boolean).join(' ')||'0 ч';
}
const routeButton=(text,action,id,extra='')=>`<button type="button" class="btn small" data-route-action="${action}" data-key="${esc(id)}" ${extra}>${esc(text)}</button>`;
function localNow34(zone){
    const parts=Object.fromEntries(new Intl.DateTimeFormat('sv-SE',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date()).map(p=>[p.type,p.value]));
    return parts.year+'-'+parts.month+'-'+parts.day+'T'+parts.hour+':'+parts.minute;
}
function activity34(j){
    const r=j.route,stops=r.stops,actual=r.events.at(-1);
    if(j.order.status==='completed'||j.order.privateStage==='completed')return 'Рейс завершён';
    if(actual){
        if(actual.action==='depart')return 'По отметке водителя · в пути в '+stops[actual.index+1].city;
        const label=actual.index===0?'на погрузке':actual.index===stops.length-1?'на выгрузке':'в городе';
        return 'По отметке водителя · '+label+' · '+stops[actual.index].city;
    }
    const now=localNow34(r.timezone),next=stops.findIndex(s=>s.eta>now);
    if(next===0){const empty=j.emptyApproach;return empty&&empty.start<=now&&now<empty.end?'По плану · порожний, подача в '+stops[0].city:'По плану · предстоит погрузка';}
    if(next<0)return 'По плану · выгрузка в '+stops.at(-1).city;
    return 'По плану · в пути в '+stops[next].city;
}
function routeStrip34(job){
    const r=job.route,stops=r.stops,actual=r.events.at(-1),o=job.order,key=job.offer.id;
    const stage=activity34(job),ab=job.emptyApproach;
    return `<div class="route-trip34">${ab?`<div class="empty-leg34"><b>Порожний</b> · ${esc(ab.origin)} → ${esc(ab.dest)} · ${km34(ab.km)}<small>${esc(time34(ab.start))} — ${esc(time34(ab.end))} · ${esc(elapsed34(ab.start,ab.end))}</small></div>`:''}
    <div class="route-heading34"><button type="button" class="d31link" data-dispatch="detail" data-id="${esc(o.id)}">${esc(o.from)} → ${esc(o.to)}</button><span class="pill ${job.confirmed?'green':'amber'}">${job.confirmed?'Назначен':'Резерв'}</span></div>
    <small>${esc(o.shipperName||o.pickupCompany||'Погрузка')} → ${esc(o.recipient||'Грузополучатель уточняется')}</small>
    <button type="button" class="strip34" data-route-action="route" data-key="${esc(key)}" aria-label="Маршрут и время · ${esc(o.from)} — ${esc(o.to)}">${stops.map((p,i)=>`<span class="stop34 ${i===0?'origin34':''} ${actual&&i<actual.index?'past34':''}"><b>${esc(p.city)}</b><small>${time34(p.eta)}</small>${i?`<small>${km34(p.km)} · ${esc(elapsed34(stops[i-1].eta,p.eta))}</small>`:'<small>Погрузка</small>'}</span>`).join('')}</button>
    <div class="route-meta34"><strong>С грузом</strong><span>${esc(stage)}</span><span>${km34(r.totalKm)}</span></div></div>`;
}
function job34(key){
    return planCache31.data?.drivers.flatMap(d=>d.jobs).find(j=>j.offer.id===key);
}
function routeDialog34(){
    const j=job34(form31.id);if(!j)return dialog29('Маршрут','<p>Обновите график и откройте рейс ещё раз.</p>');
    const r=j.route,o=j.order,key=j.offer.id,editable=state.user.role==='driver'&&r.editable;
    const last=r.events.at(-1),next=last?(last.action==='arrive'?last.index:last.index+1):0;
    const action=last?.action==='arrive'?'depart':'arrive';
    return dialog29('Маршрут и время',`<h3>${esc(o.from)} → ${esc(o.to)}</h3><p>${esc(activity34(j))}</p>
       <p class="route-caption34">${km34(r.totalKm)} · ${esc(r.timezone)} · плановый темп ${r.dailyKm} км/сут. Выгрузка по заявке · ${shortDate31(r.due)}</p>
       <ol class="route-list34">${r.stops.map((p,i)=>{
          const event=r.events.filter(e=>e.index===i).at(-1);
          return `<li class="${i===0?'origin34':''}"><b>${esc(p.city)}</b> · ${i===0?'погрузка':i===r.stops.length-1?'выгрузка':'в пути'}<small>${esc(i===0?[o.shipperName,o.pickupAddress].filter(Boolean).join(' · '):i===r.stops.length-1?[o.recipient,o.deliveryAddress].filter(Boolean).join(' · '):'')}</small><small>По плану ${time34(p.eta)}${i?' · '+km34(p.km)+' · '+elapsed34(r.stops[i-1].eta,p.eta)+' · '+esc(p.method):''}</small>
          ${event?`<small>Водитель ${event.action==='arrive'?'прибыл':'выехал'} · ${time34(event.at)}</small>`:''}
          ${editable&&i>0&&(!last||i>last.index)?`<details><summary>Изменить маршрут</summary>
          <form data-route-change="eta" data-i="${i}"><label class="field">Дата и время прибытия<input type="datetime-local" name="eta" required value="${p.eta}"></label><button class="btn small" type="submit">Сдвинуть следующие пункты</button></form>
          <form data-route-change="via" data-i="${i}"><label class="field">Город перед этим пунктом<input name="city" required minlength="2" maxlength="120"></label><button class="btn small" type="submit">Добавить город</button></form></details>`:''}
          ${editable&&j.offer.status==='selected'&&i===next&&!(i===r.stops.length-1&&action==='depart')?routeButton(action==='arrive'?'Прибыл':'Выехал',action,key,'data-i="'+i+'"'):''}</li>`;
       }).join('')}</ol><p role="alert" class="d31error" data-route-error></p>`,
       button31('Открыть заявку','detail',o.id)+'<button class="btn" data-close>Закрыть</button>');
}
async function changeRoute34(key,body){
    const j=job34(key),uid=state.user.id;
    if(!j)return;
    const result=await api('/api/driver/routes/'+encodeURIComponent(key),'PUT',{...body,version:j.route.version});
    if(state.user?.id!==uid)return;
    j.route=result.route;
    await loadState({fromEvent:true});
    if(state.modal==='dispatch:route'&&form31.id===key)render();
}
function compactLane34(row,dates){
    const start=dates[0],end=dates.at(-1),count=dates.length;
    const column=d=>Math.max(0,Math.min(count-1,Math.round((Date.parse(d+'T12:00Z')-Date.parse(start+'T12:00Z'))/86400000)));
    function band(a,b,text,kind,attrs){
        if(b<start||a>end)return '';
        return `<div class="lane-row34" style="--days:${count}"><button type="button" class="band34 ${kind}" style="grid-column:${column(a)+1}/${column(b)+2}" ${attrs} title="${esc(text)}">${esc(text)}</button></div>`;
    }
    const tracks=row.jobs.map(j=>{
        const ab=j.emptyApproach,r=j.route,st=r.stops,key=esc(j.offer.id);
        const attrs='data-route-action="route" data-key="'+key+'"';
        return (ab?band(ab.start.slice(0,10),ab.end.slice(0,10),'Порожний · '+ab.origin+' → '+ab.dest,'deadhead34',attrs):'')+
            band(st[0].eta.slice(0,10),st.at(-1).eta.slice(0,10),'С грузом · '+j.order.from+' → '+j.order.to,'loaded34',attrs);
    }).join('');
    const absences=(row.leave||[]).map(l=>band(l.start,l.end,'Отпуск','away34',state.user.role==='driver'?'data-route-action="leaveList" data-key=""':'disabled')).join('');
    const outside=(row.busyOutside||[]).map(d=>band(d,d,'Занят','away34','disabled')).join('');
    return tracks+absences+outside||'<small class="d31empty">Рейсы на этот период не запланированы</small>';
}
async function openLeave34(){
    const uid=state.user.id,rows=await api('/api/driver/leave');
    if(state.user?.id!==uid)return;
    openDispatch('leaveList');form31.rows=rows;render();
}
function leaveList34(){
    const rows=form31.rows||[];
    return dialog29('Отпуск',rows.map(l=>`<div class="leave-row34"><b>${esc(localeDay(l.start))} — ${esc(localeDay(l.end))}</b>
        ${routeButton('Изменить даты','editLeave',String(l.id))}${button31('Убрать отпуск','removeLeave',l.id)}</div>`).join('')||'<p>Отпусков в графике пока нет.</p>',button31('Внести отпуск','leave')+'<button class="btn" data-close>Закрыть</button>');
}
function choiceTable34(rows){
    const headers=['Статус заявки','Менеджер','Дата погрузки','Грузополучатель','Продукт','Масса, т','Тип авто','Тип отгрузки','Перевозчик','Водитель','Город доставки'];
    const status=o=>({completed:'Завершена',cancelled:'Отменена',enroute:'В пути',confirmed:'Назначена',choice:'Подтверждена'}[o.status]||(o.reviewOnly||o.searchLaunched?'Отправлена':'Создана'));
    return `<div class="d31scroll"><table class="journal34"><thead><tr>${headers.map(v=>'<th>'+v+'</th>').join('')}</tr></thead><tbody>${rows.map(o=>{
        const values=[o.managerSurname||o.manager||o.author,shortDate31(o.date)+' '+o.time,o.recipient,o.product,fmt(o.tons),o.vehicleType==='Автоцистерна'?'А/ц':o.vehicleType,o.mode,o.carrier,o.driver,o.to];
        return `<tr><td>${button31(status(o),'detail',o.id)}</td>${values.map(v=>'<td>'+esc(v||'—')+'</td>').join('')}</tr>`;
    }).join('')}</tbody></table></div>`;
}
async function candidates34(ref){
    const host=document.querySelector('[data-candidates34]'),uid=state.user.id;
    if(!host||host.dataset.loaded)return;
    host.dataset.loaded='1';
    try{
        const {items}=await api('/api/fleet/candidates/'+encodeURIComponent(ref));
        if(!host.isConnected||state.user?.id!==uid)return;
        host.innerHTML=items.length?'<b>Доступные экипажи</b>'+items.map(x=>routeButton(x.name+' · '+shortDate31(x.pickup)+(x.delay?' · позже на '+x.delay+' дн.':' · в срок')+' · '+(x.availableCity?x.availableCity+' · '+km34(x.emptyKm)+' до подачи':'местоположение не указано'),'candidate',String(x.driverId),'data-day="'+x.pickup+'" data-end="'+x.delivery+'"')).join(''):'Нет подходящих экипажей на ближайшие 30 дней.';
    }catch(err){if(host.isConnected){host.textContent=err.message;delete host.dataset.loaded}}
}
document.addEventListener('click',async e=>{
    const b=e.target.closest('[data-route-action]');if(!b||!state.user)return;
    e.preventDefault();e.stopImmediatePropagation();
    const k=b.dataset.key,action=b.dataset.routeAction;
    try{
        if(action==='route'){openDispatch('route',k);return}
        if(action==='leaveList'){await openLeave34();return}
        if(action==='editLeave'){
            const row=form31.rows?.find(l=>l.id===Number(k));if(!row)return;
            openDispatch('editLeave',k);form31.leave=row;render();return;
        }
        if(action==='candidate'){
            const f=b.closest('form');f.elements.driver.value=k;f.elements.driver.dispatchEvent(new Event('change',{bubbles:true}));
            f.elements.day.value=b.dataset.day;f.elements.deliveryDay.value=b.dataset.end;f.elements.endDate.value=b.dataset.end;return;
        }
        if(action==='arrive'||action==='depart'){b.disabled=true;await changeRoute34(k,{index:Number(b.dataset.i),action});}
    }catch(err){const box=document.querySelector('[data-route-error]');if(box)box.textContent=err.message;else errorToast(err)}
    finally{if(b.isConnected)b.disabled=false}
},true);
document.addEventListener('submit',async e=>{
    const f=e.target;if(!f.dataset.routeChange)return;
    e.preventDefault();e.stopImmediatePropagation();
    const btn=f.querySelector('button');if(btn.disabled)return;btn.disabled=true;
    try{await changeRoute34(form31.id,{action:f.dataset.routeChange,index:Number(f.dataset.i),...Object.fromEntries(new FormData(f))});}
    catch(err){const box=document.querySelector('[data-route-error]');if(box)box.textContent=err.message}
    finally{if(btn.isConnected)btn.disabled=false}
},true);
