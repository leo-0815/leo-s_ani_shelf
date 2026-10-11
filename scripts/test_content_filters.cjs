const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const root = path.join(__dirname, '..');
const context = vm.createContext({window:{}, URLSearchParams, URL, Set, Date, Number, Map, Promise});
vm.runInContext(fs.readFileSync(path.join(root,'web/content-filters.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(root,'web/guest.js'),'utf8'),context);
const filters = context.window.AniShelfContentFilters, guest = context.window.AniShelfGuest;
const rows = [
  {id:1,content_rating:'general',bl_category:null},
  {id:2,content_rating:'general',bl_category:'BL漫畫'},
  {id:3,content_rating:'restricted_18',bl_category:''},
  {id:4,content_rating:'restricted_18',bl_category:'BL小說'},
  {id:5,content_rating:'unknown',bl_category:null},
].map(book=>({...book,title:'測試書',publisher_code:'chingwin',media_type:'manga',release_date:'2026-10-10'}));
for (const [value, expected] of [['',[1,2,3,4,5]],['general',[1,5]],['bl',[2,4]],['r18',[3,4]],['bl_r18',[4]],['bl,r18',[2,3,4]],['general,bl',[1,2,4,5]],['none',[]]]) {
  assert.deepEqual(rows.filter(book=>filters.matches(book,value)).map(book=>book.id),expected);
  const result=guest.filterBooks(rows,new URLSearchParams({content_filter:value,limit:'1',offset:'1'}));
  assert.equal(result.total,expected.length);
  assert.deepEqual(Array.from(result.items,book=>book.id),expected.slice(1,2));
}
for (const [mode, allowed] of [['general',['general']],['bl',['general','bl']],['r18',['general','r18']],['all',['general','bl','r18']]]) {
  assert.deepEqual(Array.from(filters.choices(mode),row=>row[0]),allowed);
  const input={value:''}, fieldset={innerHTML:''}, summary={textContent:''};
  filters.render(input,fieldset,summary,mode);
  assert.deepEqual(Array.from(fieldset.innerHTML.matchAll(/value="([^"]+)"/g),m=>m[1]),allowed);
  assert.doesNotMatch(fieldset.innerHTML,/disabled|偏好未開啟|非 BL/);
  assert.equal(filters.encode(allowed,mode),'');
  assert.equal(filters.encode([],mode),'none');
}
const input={value:'bl,r18'}, fieldset={innerHTML:''}, summary={textContent:''};
filters.render(input,fieldset,summary,'all'); assert.equal(input.value,'bl,r18'); assert.equal(summary.textContent,'BL、R18');
filters.render(input,fieldset,summary,'bl'); assert.equal(input.value,'bl'); assert.doesNotMatch(fieldset.innerHTML,/value="r18"/);
filters.render(input,fieldset,summary,'general'); assert.equal(input.value,''); assert.equal(summary.textContent,'一般向');
input.value='none';filters.render(input,fieldset,summary,'all');assert.equal(input.value,'none');assert.equal(summary.textContent,'未選分類');
for(const bad of ['male','bl,','bl,bl','none,bl','general,bl,r18,bl_r18']) assert.throws(()=>filters.matches(rows[0],bad),/無效/);
const html=fs.readFileSync(path.join(root,'web/index.html'),'utf8');
assert.match(html,/id="contentFilter" type="hidden"/);
assert.match(html,/id="contentPicker"/);
assert.match(html,/id="contentSummary" aria-label="內容分類"/);
assert.ok(html.indexOf('/content-filters.js')<html.indexOf('/guest.js'));
console.log('Content checkbox union, hidden preference choices, reset and guest pagination OK');
