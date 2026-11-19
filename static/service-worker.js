/* No private pages, API responses or credentials are cached. */
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
function foregroundDelivery29(client,data){return new Promise(resolve=>{
  const channel=new MessageChannel();let finished=false;
  const finish=handled=>{if(finished)return;finished=true;clearTimeout(timer);channel.port1.close();resolve(handled)};
  const timer=setTimeout(()=>finish(false),800);
  channel.port1.onmessage=event=>finish(event.data?.handled===true);
  try{client.postMessage({type:'logistik-push',userId:data.userId,notificationId:data.notificationId},[channel.port2])}catch{finish(false)}
})}
async function deliverPush29(data){
  if(!data||!Number.isInteger(data.notificationId)||!Number.isInteger(data.userId))return;
  const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
  const visible=windows.filter(client=>client.visibilityState==='visible');
  if(visible.length&&(await Promise.all(visible.map(client=>foregroundDelivery29(client,data)))).some(Boolean))return;
  const tag='logistik-'+data.userId+'-'+data.notificationId;
  if((await self.registration.getNotifications({tag})).length)return;
  await self.registration.showNotification('Логистик — новое уведомление',{
    body:data.body||'Откройте личный кабинет',tag,
    renotify:false,silent:false,requireInteraction:true,vibrate:[300,120,300,120,500],
    data:{url:'/',userId:data.userId,notificationId:data.notificationId}
  });
}
self.addEventListener('push',event=>event.waitUntil((async()=>{
  let data;try{data=event.data.json()}catch{return}
  await deliverPush29(data);
})()));
self.addEventListener('notificationclick',event=>{
  event.notification.close();event.waitUntil((async()=>{
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    const client=windows.find(item=>new URL(item.url).origin===self.location.origin);
    if(client){await client.focus();client.postMessage({type:'logistik-push',userId:event.notification.data?.userId})}
    else await self.clients.openWindow('/');
  })());
});
