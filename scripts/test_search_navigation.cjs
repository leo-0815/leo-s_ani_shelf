const fs=require('node:fs'), vm=require('node:vm'), path=require('node:path'), assert=require('node:assert/strict');
const root=path.join(__dirname,'..'), nodes=new Map(), events={};
function node(id){
  if(!nodes.has(id)) {
    const classes=new Set();
    nodes.set(id,{value:'keep query',events:{},rect:{top:80,bottom:120,height:40},
      classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:name=>classes.has(name)},
      appendChild(child){child.parentElement=this},addEventListener(name,fn){this.events[name]=fn},
      getBoundingClientRect(){return this.rect},scrollIntoView(){this.scrolled=true},focus(options){this.focused=true;this.focusOptions=options}});
  }
  return nodes.get(id);
}
let view='home', ready=true, modal=false, switches=[], position='sticky', reduced=false, scrolls=[];
const context=vm.createContext({window:{innerHeight:800,scrollY:700,scrollTo:options=>scrolls.push(options),matchMedia:()=>({matches:reduced})},
  document:{getElementById:node,querySelector:()=>modal?{}:null,addEventListener:(name,fn)=>events[name]=fn},
  Set,getComputedStyle:()=>({position})});
vm.runInContext(fs.readFileSync(path.join(root,'web/search-navigation.js'),'utf8'),context);
const api=context.window.AniShelfSearchNavigation;
api.init({getView:()=>view,ready:()=>ready,switchView:next=>{switches.push(next);view=next;api.mount(next)}});
for(const current of ['library','wishlist','collection','series','home','notifications']) {
  view=current;api.mount(view);
  assert.equal(node('searchBox').parentElement,node(current==='library'?'catalogSearchSlot':'headerSearchSlot'));
  assert.equal(node('catalogSearchTools').classList.contains('is-search-docked'),current==='library');
  assert.equal(node('catalogSearchTools').classList.contains('hidden'),!['library','wishlist','collection','series'].includes(current));
  assert.equal(node('searchBox').value,'keep query','moving DOM never resets search');
}
function shortcut(extra={}){let prevented=false;events.keydown({key:'k',ctrlKey:true,preventDefault(){prevented=true},...extra});return prevented;}
view='home';assert.equal(shortcut(),true);assert.equal(node('homeSearchInput').focused,true);
node('homeSearchInput').rect={top:-400,bottom:-360};shortcut();assert.equal(node('homeSearchInput').scrolled,true);
view='library';assert.equal(shortcut({ctrlKey:false,metaKey:true,key:'K'}),true);assert.equal(node('searchInput').focused,true);
assert.equal(node('searchInput').scrolled,undefined,'visible sticky search must not jump the page');
view='notifications';shortcut();assert.deepEqual(switches,['library']);
modal=true;assert.equal(shortcut(),false);modal=false;ready=false;assert.equal(shortcut(),false);ready=true;
assert.equal(shortcut({isComposing:true}),false);assert.equal(shortcut({altKey:true}),false);assert.equal(shortcut({key:'x'}),false);
node('catalogToolbar').rect={top:-500,bottom:-200,height:300};node('catalogSearchTools').rect.height=100;
node('showCatalogFilters').events.click();assert.equal(scrolls.at(-1).top,76);assert.equal(scrolls.at(-1).behavior,'smooth');
assert.equal(node('typeFilter').focusOptions.preventScroll,true);
reduced=true;position='static';node('showCatalogFilters').events.click();assert.equal(scrolls.at(-1).top,188);assert.equal(scrolls.at(-1).behavior,'auto');
view='wishlist';const count=scrolls.length;node('showCatalogFilters').events.click();assert.equal(scrolls.length,count);
const html=fs.readFileSync(path.join(root,'web/index.html'),'utf8'), css=fs.readFileSync(path.join(root,'web/styles.css'),'utf8');
assert.match(html,/data-view="library"><span>▦<\/span>書目查詢/);
assert.equal((html.match(/id="searchInput"/g)||[]).length,1);
assert.equal((html.match(/aria-keyshortcuts="Control\+k Meta\+k"/g)||[]).length,2);
assert.ok(html.indexOf('/search-navigation.js')<html.indexOf('/app.js'));
assert.match(css,/max-height: 450px/);assert.match(css,/\.catalog-search-tools\.is-search-docked \{ position: sticky/);
console.log('Search docking, Ctrl/Cmd K, modal/login guards, preserved query and filter focus OK');
