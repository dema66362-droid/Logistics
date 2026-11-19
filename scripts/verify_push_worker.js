/* Delivery decision tests: a merely visible tab must not swallow an alert. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {MessageChannel}=require('node:worker_threads');
const source=fs.readFileSync('static/service-worker.js','utf8');

async function scenario({windows=[],existing=false,data={notificationId:17,userId:3}}={}){
  const shown=[];
  const context=vm.createContext({MessageChannel,setTimeout,clearTimeout,URL,
    self:{addEventListener(){},clients:{matchAll:async()=>windows},
      registration:{getNotifications:async()=>existing?[{}]:[],showNotification:async(...args)=>shown.push(args)}}});
  vm.runInContext(source,context);
  await context.deliverPush29(data);
  return shown;
}
const tab=(handled,visibilityState='visible')=>({visibilityState,postMessage(data,ports){
  assert.equal(data.type,'logistik-push');assert.equal(data.userId,3);
  if(handled!==undefined)ports[0].postMessage({handled});
  ports[0].close();
}});
(async()=>{
  assert.equal((await scenario({windows:[tab(true)]})).length,0,'unlocked correct account owns delivery');
  assert.equal((await scenario({windows:[tab(false)]})).length,1,'wrong account or suspended audio falls back to OS');
  assert.equal((await scenario({windows:[tab(undefined)]})).length,1,'unresponsive page falls back after bounded timeout');
  assert.equal((await scenario({windows:[tab(true,'hidden')]})).length,1,'hidden page does not suppress OS notification');
  assert.equal((await scenario({windows:[tab(false),tab(true)]})).length,0,'one capable visible tab is sufficient');
  assert.equal((await scenario({existing:true})).length,0,'a displayed delivery is not sounded again');
  assert.equal((await scenario({data:{notificationId:'17',userId:3}})).length,0,'malformed payload is ignored');
  const [title,options]=(await scenario())[0];
  assert.equal(title,'Логистик — новое уведомление');assert.equal(options.silent,false);
  assert.equal(options.renotify,false);assert.equal(options.data.url,'/');
  assert.equal(options.tag,'logistik-3-17');
  console.log('Web Push worker: 8 delivery, fallback, deduplication and payload checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
