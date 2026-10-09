const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.join(__dirname, '..');

function element() {
  const classes = new Set();
  return {textContent:'', innerHTML:'', value:'', open:false, dataset:{}, events:{},
    classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),toggle:(x,on)=>on?classes.add(x):classes.delete(x),contains:x=>classes.has(x)},
    addEventListener(event, fn){this.events[event]=fn}, setAttribute(){}, focus(){this.focused=true},
    querySelectorAll(){return []}};
}

(async () => {
  const nodes = new Map();
  const get = id => {if(!nodes.has(id)) nodes.set(id,element()); return nodes.get(id)};
  const events = {}, requests = [], historyCalls = [];
  const views = ['home','library','series','upcoming','wishlist','collection','recommendations','notifications','preferences','quality','sources'];
  const nav = views.map(view => Object.assign(element(),{dataset:{view},textContent:view}));
  const sections = views.filter(x=>!['wishlist','collection'].includes(x)).map(x=>get(x+'View'));
  const document = {getElementById:get, querySelector:s=>get(s.replace(/^#/,'')),
    querySelectorAll:s=>s==='.nav-item'?nav:s==='.view-section'?sections:[], addEventListener:(e,f)=>events[e]=f};
  const location={hash:''};
  const history={pushState:(a,b,url)=>{historyCalls.push(['push',url]);location.hash=url},replaceState:(a,b,url)=>{historyCalls.push(['replace',url]);location.hash=url}};
  const ctx=vm.createContext({document,window:{addEventListener:(e,f)=>events[e]=f},location,history,
    console,Intl,Date,URLSearchParams,Set,setTimeout,clearTimeout,setInterval,clearInterval,requests});
  vm.runInContext(fs.readFileSync(path.join(root,'web/home.js'),'utf8'),ctx);
  let source=fs.readFileSync(path.join(root,'web/app.js'),'utf8');
  source=source.slice(0,source.lastIndexOf('\nloadAll()'));
  vm.runInContext(source,ctx);
  const cloud=source.includes('async function loadSession()');
  vm.runInContext(`api=async path=>{requests.push(path);return {counts:{wishlist:2,collection:3,followed_series:1},items:[],date:'2030-10-09'}};
    loadPublishers=async()=>requests.push('publishers');loadStats=async()=>requests.push('stats');loadBooks=async()=>requests.push('books');loadSeries=async()=>requests.push('series');loadLatestJob=async()=>{};
    ${cloud?"loadSession=async()=>{state.user={display_name:'<Leo>',is_admin:false,general_audience:true};return true}":"checkHealth=async()=>true;loadPreferences=async()=>{}"}`,ctx);
  await ctx.loadAll();
  assert.equal(location.hash,'#home');
  assert.deepEqual(requests,['/api/home']);
  assert.equal(get('catalogStats').classList.contains('hidden'),true);
  if(cloud) assert.equal(get('homeGreeting').textContent,'<Leo>，歡迎回到書架。');
  await ctx.switchView('library');
  assert.deepEqual(requests.slice(1),['publishers','stats','books']);
  assert.equal(historyCalls.at(-1)[0],'push');
  assert.equal(get('catalogStats').classList.contains('hidden'),false);
  location.hash='#home'; await events.hashchange();
  assert.equal(get('homeView').classList.contains('hidden'),false);
  get('homeSearchInput').value='  部分書名  ';
  get('homeSearchForm').events.submit({preventDefault(){}});
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(location.hash,'#library');
  assert.equal(get('searchInput').value,'部分書名');
  await ctx.switchView('home');
  events.keydown({ctrlKey:true,key:'k',preventDefault(){}});
  assert.equal(get('homeSearchInput').focused,true);
  await ctx.switchView('invalid');assert.equal(location.hash,'#home');
  if(cloud){await ctx.switchView('quality');assert.equal(location.hash,'#home')}
  await ctx.window.AniShelfHome.load({api:async()=>{throw Error('secret detail')},user:null,openBook(){}});
  assert.match(get('homeUpcoming').innerHTML,/重試摘要/);
  assert.doesNotMatch(get('homeUpcoming').innerHTML,/secret detail/);
  await ctx.window.AniShelfHome.load({api:async()=>({counts:{wishlist:1,collection:0,followed_series:0},date:'2030-10-09',items:[{id:7,title:'<script>',author:'<img>',publisher_name:'出版社',cover_url:'javascript:bad',release_date:'2030-10-09'}]}),user:null,openBook(){}});
  assert.match(get('homeUpcoming').innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(get('homeUpcoming').innerHTML,/javascript:bad|<script>|<img>/);
  const html=fs.readFileSync(path.join(root,'web/index.html'),'utf8');
  assert.match(html,/class="brand" href="#home"/);
  assert.match(html,/id="homeView"/);
  const shortcuts=html.slice(html.indexOf('class="home-shortcuts"'),html.indexOf('class="home-releases"'));
  assert.equal((shortcuts.match(/<a href="#/g)||[]).length,6);
  requests.length=0;location.hash='#library';await ctx.loadAll();
  assert.equal(location.hash,'#library');assert.deepEqual(requests,['books']);
  console.log('Home UI/routing tests OK ('+(cloud?'cloud':'local')+')');
})().catch(e=>{console.error(e);process.exit(1)});
