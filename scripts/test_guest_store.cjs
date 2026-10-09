const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const copy=v=>JSON.parse(JSON.stringify(v));
let saved,fail=false,tail=Promise.resolve();
// Small transactional IDB double: browser verification separately exercises
// the real implementation. Aborted/failed writes never publish staged state.
const db={createObjectStore(){},close(){},transaction(_name,mode){
  const tx={aborted:false,abort(){this.aborted=true;queueMicrotask(()=>this.onabort?.());}};
  tx.objectStore=()=>({get(){
    const request={};const run=async()=>{
      request.result=saved?copy(saved):undefined;request.onsuccess?.();
      await Promise.resolve();
      if(tx.aborted)return;
      if(fail && mode==='readwrite'){tx.onerror?.();return;}
      if(tx.staged)saved=copy(tx.staged);
      tx.oncomplete?.();
    };
    tail=tail.then(run);return request;
  },put(value){tx.staged=copy(value);}});
  return tx;
}};
const indexedDB={open(){const req={};queueMicrotask(()=>{req.result=db;req.onupgradeneeded?.();req.onsuccess?.();});return req;}};
const context=vm.createContext({window:{indexedDB},indexedDB,URL,URLSearchParams,Intl,Date,Set,Map,JSON,Number,Promise});
vm.runInContext(fs.readFileSync(path.join(__dirname,'..','web','guest.js'),'utf8'),context);
const guest=context.window.AniShelfGuest;
const catalog=[1,2,3].map(id=>({id,title:'測試書 '+id,series_title:'測試書',series_key:'series',publisher_code:'test',publisher_name:'測試出版社',media_type:'novel',release_status:id===3?'scheduled':'available',release_date:'2026-10-20',release_precision:'day',content_rating:'general',bl_category:''}));
const requests=[];
async function transport(url,options={}){
  requests.push([url,options]);
  const u=new URL(url,'https://test.invalid');
  if(u.pathname==='/api/guest/book-batch')return {items:catalog.filter(b=>u.searchParams.get('ids').split(',').includes(String(b.id)))};
  if(u.pathname==='/api/guest/series/detail')return {items:copy(catalog),publisher_code:'test',series_title:'測試書',series_key:'series',media_type:'novel',has_more:false};
  if(u.pathname==='/api/guest/resolve')return {items:copy(catalog),has_more:false,next_after_id:3};
  if(u.pathname.match(/^\/api\/guest\/books\/\d+$/))return copy(catalog.find(b=>b.id===Number(u.pathname.split('/').at(-1))));
  if(u.pathname==='/api/guest/stats')return {total:3,scheduled:1,available:2,unknown:0,scheduled_undated:0};
  throw Error('Unexpected network '+url);
}
const write=(p,payload)=>guest.api(p,{method:'POST',body:JSON.stringify(payload)},transport);
(async()=>{
  const initial=await guest.read();assert.equal(initial.preferences.general_audience,true);
  const bad=guest.backup(initial);bad.data.books['__proto__']={id:1};
  // JSON parsed prototype-like keys are never accepted as book IDs.
  assert.throws(()=>guest.parseBackup(JSON.parse('{"format":"anishelf-guest","version":1,"data":{"version":1,"books":{"__proto__":{}},"wishlist":{},"collection":{},"custom":{},"follows":[],"dismissed":[]}}')),/識別碼/);
  assert.throws(()=>guest.parseBackup({format:'anishelf-guest',version:9}),/備份/);
  await write('/api/wishlist/1',{state:'purchased',notes:'KEEP',paid_price:123});
  await guest.api('/api/wishlist/1',{method:'DELETE'},transport);
  let data=await guest.read();assert.equal(data.wishlist[1],undefined);assert.equal(data.collection[1].notes,'KEEP');
  const owned=await guest.api('/api/books?collection=1',{},transport);
  assert.equal(owned.total,1);assert.equal(owned.items[0].is_owned,true);
  assert.equal(owned.items[0].wishlist_state,null);
  await write('/api/series/follow',{publisher:'test',series_title:'測試書',media_type:'novel',scope:'all',following:true});
  data=await guest.read();assert.ok(data.wishlist[2]);assert.ok(data.wishlist[3]);assert.equal(data.wishlist[1],undefined);
  await guest.api('/api/wishlist/2',{method:'DELETE'},transport);
  guest.reset();await guest.api('/api/home',{},transport);
  assert.equal((await guest.read()).wishlist[2],undefined,'refresh must not re-add explicitly removed tracked books');
  await write('/api/series/follow',{publisher:'test',series_title:'測試書',media_type:'novel',following:false});
  data=await guest.read();assert.equal(data.follows.length,0);assert.ok(data.collection[1]);assert.ok(data.wishlist[3]);
  await write('/api/collection/custom',{title:'私人書',notes:'manual',media_type:'manga'});
  const backup=guest.parseBackup(guest.backup(await guest.read()));
  const current=guest.validate(await guest.read());current.collection[1].notes='newer';
  guest.merge(current,backup);assert.equal(current.collection[1].notes,'newer');assert.equal(Object.keys(current.custom).length,1);
  const payload=guest.payload(current);assert.equal(payload.collection[0].book_id,1);assert.equal(payload.custom[0].title,'私人書');
  for(const [url,opts] of requests){assert.ok(url.startsWith('/api/guest/'));assert.ok(!(opts.body||'').includes('KEEP'));assert.ok(!(opts.body||'').includes('manual'));}
  const before=JSON.stringify(await guest.read());fail=true;
  await assert.rejects(write('/api/collection/custom',{title:'不得假裝保存'}),/未儲存/);
  fail=false;assert.equal(JSON.stringify(await guest.read()),before);
  await assert.rejects(write('/api/collection/custom',{title:'bad',paid_price:-1}),/價格/);
  await assert.rejects(guest.api('/api/update',{method:'POST',body:'{}'},transport),/Google/);
  guest.reset();let renewals=0,reads=0;
  const renew=async(url)=>{
    if(url==='/api/guest/start'){renewals++;return {};}
    if(++reads===1){const e=new Error('expired');e.guestExpired=true;throw e;}
    return copy(catalog[0]);
  };
  await guest.api('/api/books/1',{},renew);assert.equal(renewals,1);assert.equal(reads,2);
  guest.reset();renewals=0;
  const stillExpired=async(url)=>{
    if(url==='/api/guest/start'){renewals++;return {};}
    const e=new Error('expired');e.guestExpired=true;throw e;
  };
  await assert.rejects(guest.api('/api/books/1',{},stillExpired),/expired/);assert.equal(renewals,1,'renewal must not loop');
  const separate=guest.empty();separate.follows=[{publisher:'test',series_key:'series',media_type:'novel',scope:'all'}];
  assert.equal(guest.decorate({...catalog[0],media_type:'manga'},separate).series_following,false);
  // Confirm a refreshed module (like a page reload) reads the same IDB state.
  vm.runInContext(fs.readFileSync(path.join(__dirname,'..','web','guest.js'),'utf8'),context);
  assert.equal((await context.window.AniShelfGuest.read()).collection[1].notes,'KEEP');
  console.log('Guest transactional storage, independent ownership, tracking, privacy, backups and reload tests OK');
})().catch(error=>{console.error(error);process.exit(1);});
