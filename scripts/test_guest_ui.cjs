const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const entryHtml=fs.readFileSync(path.join(__dirname,'..','web','index.html'),'utf8');
assert.match(entryHtml,/id="guestLogin"[^>]*aria-describedby="guestStorageWarning"/);
assert.match(entryHtml,/id="guestStorageWarning" role="note"/);
assert.match(entryHtml,/Cookie 僅保存訪客識別/);
assert.match(entryHtml,/IndexedDB/);
const nodes=new Map(),calls=[],saved={version:1,wishlist:{1:{notes:'private'}},collection:{},custom:{},follows:[],decisions:{}};
function element(id){return {disabled:false,textContent:'',innerHTML:'',events:{},open:false,addEventListener(e,f){this.events[e]=f},showModal(){this.open=true},close(){this.open=false},cloneNode(){return Object.assign(element(id),{textContent:this.textContent,disabled:this.disabled})},replaceWith(other){nodes.set(id,other)}};}
const $=id=>{if(!nodes.has(id))nodes.set(id,element(id));return nodes.get(id);};
const context=vm.createContext({window:{AniShelfGuest:{read:async()=>saved,payload:data=>({version:1,wishlist:[{book_id:1,notes:data.wishlist[1].notes}]}),mutate:async fn=>fn(saved)}},
  state:{user:{id:7,is_guest:false},view:'home'},$,$$:()=>[],location:{},document:{},history:{},Date,URL,Blob,setTimeout,
  toast(){},switchView:async()=>{},api:async(path,options)=>{
    calls.push([path,JSON.parse(options.body)]);
    return path.endsWith('preview')?{summary:{new:1,wishlist:1,collection:0,custom:0,follows:0,existing:0,missing:0},token:'confirmation'}:{summary:{new:1}};
  }});
vm.runInContext(fs.readFileSync(path.join(__dirname,'..','web','guest-ui.js'),'utf8'),context);
(async()=>{
  const ui=context.window.AniShelfGuestUi;
  await ui.offer(false);assert.equal(calls.length,0,'login invitation must not transmit private notes');
  assert.equal($('#guestImportDialog').open,true);
  await $('#guestImportReview').events.click();
  assert.equal(calls.length,1);assert.equal(calls[0][0],'/api/guest-import/preview');
  assert.equal(calls[0][1].data.wishlist[0].notes,'private');
  assert.match($('#guestImportReview').textContent,/確認匯入/);
  await $('#guestImportReview').events.click();
  assert.equal(calls[1][0],'/api/guest-import/apply');assert.equal(calls[1][1].token,'confirmation');
  assert.equal(saved.wishlist[1].notes,'private','cloud import must retain browser original');
  assert.equal(saved.decisions[7],'imported');
  await ui.offer(false);assert.equal($('#guestImportDialog').open,false);
  await ui.offer(true);await $('#guestImportLater').events.click();assert.equal(saved.decisions[7],'later');
  await ui.offer(true);await $('#guestImportReview').events.click();
  const before=calls.length;context.state.user.id=8;
  await $('#guestImportReview').events.click();assert.equal(calls.length,before,'account changes invalidate UI confirmation');
  console.log('Guest import UI explicit opt-in, preview, confirmation, original retention and account isolation tests OK');
})().catch(e=>{console.error(e);process.exit(1)});
