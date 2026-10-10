const {chromium} = require("playwright");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
  const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL || "msedge", headless: true});
  const page = await browser.newPage({viewport: {width:1280, height:900}});
  const errors = [], requests = [];
  page.on("pageerror", error => errors.push(error.message));
  const publishers = ["kadokawa","spp","tongli","chingwin","tohan","chonghong"].map((code,i)=>({code,name:["台灣角川","尖端","東立","青文","台灣東販","長鴻"][i],enabled:true,book_count:60}));
  const books = publishers.flatMap((p,i)=>Array.from({length:60},(_,n)=>({id:i*60+n+1,title:p.name+" 測試小說 "+n,
    publisher_code:p.code,publisher_name:p.name,author:"作者",media_type:"novel",release_status:"available",
    release_date:"2026-10-04",edition_type:"standard",series_title:"系列"+n,wishlist_state:"wanted",is_owned:true,
    content_rating:p.code==="chingwin"?(n<15?"general":n<30?"restricted_18":"unknown"):"unknown",
    bl_category:p.code==="chingwin" && n%2===1?"BL漫畫":"",
    rating_source:n<15?"publisher":"unknown",rating_confidence:n<15?100:0})));
  let mode = process.env.ANISHELF_UI_DEFAULT_ONLY === "1" ? "general" : "all";
  function filtered(params) {
    const codes = params.get("publishers");
    return books.filter(b=>(!['general','bl'].includes(mode) || b.content_rating!=="restricted_18") &&
      (!['general','r18'].includes(mode) || !b.bl_category) && (!codes || codes.split(",").includes(b.publisher_code)) &&
      (!params.get("q") || b.title.includes(params.get("q"))));
  }
  await page.route("**/*", async route => {
    const req=route.request(), u=new URL(req.url()), p=u.pathname;
    const json=data=>route.fulfill({contentType:"application/json",body:JSON.stringify(data)});
    if (!p.startsWith("/api/")) {
      const file=p==="/"? "index.html":p.slice(1);
      if (!["index.html","app.js","home.js","guest.js","guest-ui.js","styles.css"].includes(file)) return route.fulfill({status:404,body:""});
      return route.fulfill({path:path.join(__dirname,"..","web",file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
    }
    if(p==="/api/auth/me") return json({authenticated:true,user:{id:7,display_name:"測試",email:"test@example.test",is_admin:false,csrf_token:"fixture",content_mode:mode,general_audience:mode==='general'}});
    if(p==="/api/health") return json({ok:true,configured:true,version:"test"});
    if(p==="/api/preferences") {
      if(req.method()==="POST") {mode=req.postDataJSON().content_mode;assert.ok(['general','bl','r18','all'].includes(mode));}
      return json({content_mode:mode,general_audience:mode==='general'});
    }
    if(p==="/api/publishers") return json({items:publishers.map(b=>({...b,book_count:filtered(new URLSearchParams('publishers='+b.code)).length}))});
    if(p==="/api/stats") return json({total:filtered(new URLSearchParams()).length,scheduled:0,available:315,unknown:0,scheduled_undated:0,wishlist:1,followed_series:1,purchased:1,purchased_paper:1,purchased_digital:0,purchased_series:1,purchased_spend:0});
    if(p==="/api/books" || p==="/api/series") {
      requests.push({path:p,params:new URLSearchParams(u.search)});
      const items=filtered(u.searchParams), offset=Number(u.searchParams.get("offset")||0), limit=Number(u.searchParams.get("limit")||100);
      return json({items:p==="/api/series"?[]:items.slice(offset,offset+limit),total:items.length});
    }
    return json({items:[]});
  });
  const latest=()=>requests.at(-1);
  async function pick(codes) {
    await page.locator("#publisherSummary").click();
    await page.locator("#publisherSelectNone").click();
    for(const code of codes) await page.locator('#publisherOptions input[value="'+code+'"]').check();
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")||r.url().includes("/api/series?")), page.locator("#publisherApply").click()]);
  }
  try {
    await page.goto("http://anishelf.test/#library");
    await page.locator(".book-card").first().waitFor();
    if(process.env.ANISHELF_UI_DEFAULT_ONLY === "1") {
      await page.locator("#publisherSummary").click();
      assert.equal(await page.locator('#publisherOptions input[value="chingwin"]').count(),1);
      await page.locator("#publisherSummary").press("Escape");
      await page.locator('[data-view="preferences"]').click();
      assert.equal(await page.locator("#contentMode").inputValue(),'general');
      await page.locator("#contentMode").selectOption('all');
      await Promise.all([page.waitForResponse(r=>r.url().includes("/api/publishers")),page.locator('#preferencesForm [type="submit"]').click()]);
      await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")),page.locator('[data-view="library"]').click()]);
      await page.locator("#publisherSummary").click();
      assert.equal(await page.locator('#publisherOptions input[value="chingwin"]').count(),1);
      assert.deepEqual(errors,[]);
      console.log("Default general-audience UI OK: checked at first load, verified Chingwin visible, opt-out keeps publisher.");
      return;
    }
    for(const count of [1,2,3,4,5]) {
      await pick(publishers.slice(0,count).map(p=>p.code));
      assert.equal(latest().params.get("publishers").split(",").length,count);
      assert.equal(latest().params.get("offset"),"0");
    }
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")), page.locator("#nextPage").click()]);
    assert(Number(latest().params.get("offset"))>0);
    await pick(["spp","tongli"]);
    assert.equal(latest().params.get("offset"),"0");
    assert.equal(await page.locator("#publisherSummary").textContent(),"已選 2 家出版社");
    if(process.env.ANISHELF_UI_SCREENSHOTS) {
      await page.locator("#publisherSummary").click();
      await page.screenshot({path:path.join(__dirname,"..","logs","pr27-publishers.png"),fullPage:false,animations:"disabled"});
      await page.locator("#publisherSummary").press("Escape");
    }
    for(const view of ["wishlist","collection","series","library"]) {
      await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")||r.url().includes("/api/series?")), page.locator('[data-view="'+view+'"]').click()]);
      assert.equal(latest().params.get("publishers"),"spp,tongli");
      assert.equal(await page.locator("#catalogFilterBar").isVisible(),true);
    }
    await pick([]);
    assert.equal(latest().params.get("publishers"),"none");
    assert.equal(await page.locator(".book-card").count(),0);
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")), page.locator("#publisherReset").click()]);
    assert.equal(latest().params.get("publishers"),null);
    await page.locator('[data-view="preferences"]').click();
    await page.locator("#contentMode").selectOption('general');
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/publishers")), page.locator('#preferencesForm [type="submit"]').click()]);
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")), page.locator('[data-view="library"]').click()]);
    await page.locator("#publisherSummary").click();
    assert.equal(await page.locator('#publisherOptions input[value="chingwin"]').count(),1);
    await page.locator("#publisherSummary").press("Escape");
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")&&r.url().includes("q=")), page.locator("#searchInput").fill("青文")]);
    assert.equal(await page.locator(".book-card").count(),23);
    assert(filtered(new URLSearchParams("q=青文")).every(b=>b.content_rating!=="restricted_18"&&!b.bl_category));
    for(const selected of ['bl','r18','all','general']) {
      await page.locator('[data-view="preferences"]').click();
      await page.locator('#contentMode').selectOption(selected);
      await Promise.all([page.waitForResponse(r=>r.url().includes('/api/publishers')),page.locator('#preferencesForm [type="submit"]').click()]);
      await Promise.all([page.waitForResponse(r=>r.url().includes('/api/books?')),page.locator('[data-view="library"]').click()]);
      assert.equal(await page.locator('.book-card').count(),{general:23,bl:45,r18:30,all:60}[selected]);
    }
    await page.reload();
    await page.locator('[data-view="preferences"]').click();
    await page.locator("#contentMode").waitFor();
    assert.equal(await page.locator("#contentMode").inputValue(),'general');
    assert.equal(await page.locator('[data-view="preferences"]').evaluate(el=>el.classList.contains("active")),true);
    if(process.env.ANISHELF_UI_SCREENSHOTS) await page.screenshot({path:path.join(__dirname,"..","logs","pr45-content-modes.png"),fullPage:false});
    await page.locator("#contentMode").selectOption('all');
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/publishers")), page.locator('#preferencesForm [type="submit"]').click()]);
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")), page.locator('[data-view="library"]').click()]);
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")), page.locator("#clearFilters").click()]);
    await page.setViewportSize({width:390,height:844});
    await page.locator("#publisherSummary").click();
    assert.equal(await page.locator('#publisherOptions input[value="chingwin"]').count(),1);
    assert.equal(await page.locator(".publisher-menu").evaluate(el=>el.getBoundingClientRect().right <= innerWidth),true);
    await page.locator("#publisherSelectNone").click();
    await page.locator('#publisherOptions input[value="spp"]').check();
    await page.locator('#publisherOptions input[value="tongli"]').check();
    await Promise.all([page.waitForResponse(r=>r.url().includes("/api/books?")), page.locator("#publisherApply").click()]);
    await page.locator('[data-view="preferences"]').click();
    assert.equal(await page.locator('#contentMode option').count(),4);
    assert.equal(await page.locator('#contentMode').evaluate(el=>el.getBoundingClientRect().right<=innerWidth),true);
    if(process.env.ANISHELF_UI_SCREENSHOTS) await page.screenshot({path:path.join(__dirname,"..","logs","pr45-content-modes-mobile.png"),fullPage:false});
    assert.deepEqual(errors,[]);
    console.log("Filters UI OK: 1-5 publishers, none/all, page reset/persistence, series/wishlist/collection, saved general mode, restoration, mobile.");
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});

