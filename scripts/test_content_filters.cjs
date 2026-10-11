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
for (const [value, expected] of [['',[1,2,3,4,5]],['general',[1,5]],['bl',[2,4]],['r18',[3,4]],['bl_r18',[4]]]) {
  assert.deepEqual(rows.filter(book=>filters.matches(book,value)).map(book=>book.id),expected);
  const result=guest.filterBooks(rows,new URLSearchParams({content_filter:value,limit:'1',offset:'1'}));
  assert.equal(result.total,expected.length);
  assert.deepEqual(Array.from(result.items,book=>book.id),expected.slice(1,2));
}
for (const [mode, allowed] of [['general',['','general']],['bl',['','general','bl']],['r18',['','general','r18']],['all',['','general','bl','r18','bl_r18']]]) {
  assert.deepEqual(Array.from(filters.options.filter(([key])=>filters.allowed(mode,key)),row=>row[0]),allowed);
}
const select={value:'bl_r18',innerHTML:''};
filters.render(select,'all'); assert.equal(select.value,'bl_r18');
filters.render(select,'general'); assert.equal(select.value,''); assert.match(select.innerHTML,/disabled/);
assert.throws(()=>filters.matches(rows[0],'male'),/無效/);
const html=fs.readFileSync(path.join(root,'web/index.html'),'utf8');
assert.match(html,/id="contentFilter" aria-label="內容分類"/);
assert.ok(html.indexOf('/content-filters.js')<html.indexOf('/guest.js'));
console.log('Content filters, overlap, unknown labels, preference choices and guest pagination OK');
