/* No private pages, API responses or credentials are cached. */
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('push',event=>event.waitUntil((async()=>{
  let data;try{data=event.data.json()}catch{return}
  if(!data||!Number.isInteger(data.notificationId)||!Number.isInteger(data.userId))return;
  const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
  const visible=windows.filter(client=>client.visibilityState==='visible');
  if(visible.length){for(const client of visible)client.postMessage({type:'logistik-push',userId:data.userId});return}
  await self.registration.showNotification('Логистик — новое уведомление',{
    body:data.body||'Откройте личный кабинет',tag:'logistik-'+data.userId+'-'+data.notificationId,
    renotify:true,silent:false,requireInteraction:true,vibrate:[300,120,300,120,500],
    data:{url:'/',userId:data.userId,notificationId:data.notificationId}
  });
})()));
self.addEventListener('notificationclick',event=>{
  event.notification.close();event.waitUntil((async()=>{
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    const client=windows.find(item=>new URL(item.url).origin===self.location.origin);
    if(client){await client.focus();client.postMessage({type:'logistik-push',userId:event.notification.data?.userId})}
    else await self.clients.openWindow('/');
  })());
});
