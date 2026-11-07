const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname,'..');
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id,{events:{},attrs:{},open:false,children:[],
    addEventListener(e,f){this.events[e]=f},setAttribute(k,v){this.attrs[k]=v},
    appendChild(child){child.parentElement=this;this.children.push(child)},prepend(child){child.parentElement=this;},
    showModal(){this.open=true;this.modalCount=(this.modalCount||0)+1},
    close(){this.open=false;this.events.close?.()},getBoundingClientRect(){return {left:0,top:0,right:320,bottom:800}}});
  return nodes.get(id);
}
const media={matches:true,addEventListener(e,fn){this.change=fn}};
const document={getElementById:node,querySelector:node,body:{style:{overflow:'auto'}}};
const window={matchMedia:q=>{assert.equal(q,'(max-width: 900px)');return media}};
const context=vm.createContext({window,document});
vm.runInContext(fs.readFileSync(path.join(root,'web/mobile-navigation.js'),'utf8'),context);
const menu=window.AniShelfMobileNavigation;
menu.init();menu.init();
const dialog=node('mobileNavDialog'),sidebar=node('siteNavigation'),open=node('mobileNavOpen');
assert.equal(sidebar.parentElement,dialog,'reuse the desktop navigation, do not clone private/admin links');
open.events.click();assert.equal(dialog.open,true);assert.equal(open.attrs['aria-expanded'],'true');
assert.equal(document.body.style.overflow,'hidden');
dialog.events.click({target:dialog,clientX:50,clientY:100});assert.equal(dialog.open,true,'inside padding is not backdrop');
dialog.events.click({target:dialog,clientX:360,clientY:100});assert.equal(dialog.open,false);
assert.equal(document.body.style.overflow,'auto');assert.equal(open.attrs['aria-expanded'],'false');
open.events.click();sidebar.events.click({target:{closest:()=>({})}});assert.equal(dialog.open,false);
open.events.click();node('mobileNavClose').events.click();assert.equal(dialog.open,false);
open.events.click();media.matches=false;media.change();
assert.equal(dialog.open,false);assert.equal(sidebar.parentElement,node('.shell'));
open.events.click();assert.equal(dialog.open,false,'desktop cannot open the mobile modal');
media.matches=true;media.change();assert.equal(sidebar.parentElement,dialog);
const html=fs.readFileSync(path.join(root,'web/index.html'),'utf8');
assert.equal((html.match(/id="siteNavigation"/g)||[]).length,1);
assert.match(html,/aria-label="頁面選單"/);assert.match(html,/aria-controls="mobileNavDialog"/);
assert.match(html,/id="mobileNavClose"[^>]+autofocus/);
const code=fs.readFileSync(path.join(root,'web/mobile-navigation.js'),'utf8');
assert.doesNotMatch(code,/fetch\(|api\(|cloneNode|innerHTML/);
console.log('Mobile dialog navigation, backdrop/selection/close/resize, scroll lock and zero-fetch tests OK');
