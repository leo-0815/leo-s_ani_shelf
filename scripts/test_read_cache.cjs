const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ctx = vm.createContext({window:{}, URLSearchParams});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/read-cache.js'),'utf8'),ctx);
const {create,group} = ctx.window.AniShelfReadCache;
let now=0;
const cache=create({now:()=>now,maxEntries:3,maxBytes:1000});
cache.set('1:general','/api/books?q=x&offset=0',{items:[{id:1}]});
const item=cache.get('1:general','/api/books?offset=0&q=x');
assert.equal(item.items[0].id,1);item.items[0].id=9;
assert.equal(cache.get('1:general','/api/books?q=x&offset=0').items[0].id,1);
for(const scope of ['2:general','1:all','guest:general']) assert.equal(cache.get(scope,'/api/books?q=x&offset=0'),undefined);
assert.equal(cache.get('1:general','/api/books?q=x&offset=0&collection=1'),undefined);
now=299999;assert.ok(cache.get('1:general','/api/books?q=x&offset=0'));
now=300000;assert.equal(cache.get('1:general','/api/books?q=x&offset=0'),undefined);
for(const p of ['/api/auth/me','/api/jobs/latest','/api/export.json','/api/calendar.ics','/api/update']) {
  assert.equal(group(p),'');cache.set('1',p,{secret:true});assert.equal(cache.get('1',p),undefined);
}
cache.set('1','/api/publishers',{items:[]});cache.set('1','/api/stats',{total:1});
const gen=cache.generation;cache.invalidate(['stats']);assert.ok(cache.generation>gen);
assert.equal(cache.get('1','/api/stats'),undefined);assert.ok(cache.get('1','/api/publishers'));
now+=899999;assert.ok(cache.get('1','/api/publishers'));now++;assert.equal(cache.get('1','/api/publishers'),undefined);
for(let i=0;i<4;i++)cache.set('1','/api/books?q='+i,{i});
assert.equal(cache.get('1','/api/books?q=0'),undefined);
cache.set('1','/api/books?q=huge',{x:'x'.repeat(1000)});assert.equal(cache.get('1','/api/books?q=huge'),undefined);
cache.clear();assert.equal(cache.get('1','/api/books?q=3'),undefined);
console.log('Read cache TTL, canonical keys, isolation, copies, invalidation and LRU OK');
