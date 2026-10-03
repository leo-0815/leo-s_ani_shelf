const {chromium} = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
  const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL || "msedge", headless:true});
  const page = await browser.newPage({viewport:{width:1280,height:900}});
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const books = [1,2].map(id=>({id,title:"測試小說 "+id,author:"測試作者",publisher_code:"spp",publisher_name:"尖端",
    series_title:"測試小說",media_type:"novel",volume_label:String(id),edition_type:"standard",
    release_date:"2026-10-03",release_precision:"day",release_status:"available",history:[],
    wishlist_state:id===1?"wanted":null,is_owned:id===1,collection_id:id===1?10:null,collection:{owned_format:"paper"}}));
  let custom = null;
  await page.route("**/*", async route => {
    const request=route.request(), url=new URL(request.url()), p=url.pathname;
    const json = data => route.fulfill({contentType:"application/json",body:JSON.stringify(data)});
    if (!p.startsWith("/api/")) {
      const file=p==="/"?"index.html":p.slice(1);
      if (!["index.html","app.js","styles.css"].includes(file)) return route.fulfill({status:404,body:""});
      return route.fulfill({path:path.join(__dirname,"..","web",file),contentType:{"index.html":"text/html","app.js":"text/javascript","styles.css":"text/css"}[file]});
    }
    if (p==="/api/auth/me") return json({authenticated:true,user:{id:1,email:"reader@example.test",display_name:"測試讀者",csrf_token:"test",is_admin:false}});
    if (p==="/api/stats") return json({total:2,scheduled:0,available:2,unknown:0,scheduled_undated:0,
      wishlist:1,followed_series:0,purchased:books.filter(b=>b.is_owned).length+(custom?1:0),
      purchased_paper:1,purchased_digital:0,purchased_series:1,purchased_spend:0});
    if (p==="/api/publishers") return json({items:[{code:"spp",name:"尖端",enabled:true,book_count:2}]});
    if (p==="/api/books") {
      let items = url.searchParams.get("collection")==="1" ? books.filter(b=>b.is_owned).concat(custom?[custom]:[]) : books;
      if (url.searchParams.get("q")) items=items.filter(b=>b.title.includes(url.searchParams.get("q")));
      return json({items,total:items.length,limit:100,offset:0});
    }
    if (/\/api\/books\/\d+\/recommendations/.test(p)) return json({items:[]});
    if (/\/api\/books\/\d+$/.test(p)) return json(books.find(b=>b.id===Number(p.split("/").pop())));
    if (/\/api\/wishlist\/\d+$/.test(p)) {books.find(b=>b.id===Number(p.split("/").pop())).wishlist_state=request.method()==="DELETE"?null:"wanted";return json({ok:true});}
    if (/\/api\/collection\/book\/\d+/.test(p)) {
      const book=books.find(b=>b.id===Number(p.split("/").pop())), payload=request.postDataJSON();
      assert.equal(payload.state,"purchased");
      assert.equal(payload.follow_scope,"all");
      book.is_owned=true;book.collection_id=10+book.id;return json({ok:true});
    }
    if(p==="/api/collection/custom") {
      const payload=request.postDataJSON();
      assert.equal(payload.title,"我的私人藏書");
      custom={...payload,id:-12,collection_id:12,is_custom:true,is_owned:true};
      return json({ok:true,collection_id:12});
    }
    return json({items:[]});
  });
  try {
    await page.goto("http://anishelf.test/#collection");
    await page.locator("#addCollectionBook").waitFor({state:"visible"});
    await page.locator('[data-wishlist="1"]').click();
    await page.waitForFunction(()=>document.querySelector('[data-id="1"]') && document.querySelector('[data-wishlist="1"]').textContent==="♡");
    assert.equal(await page.locator('[data-id="1"]').count(),1);
    await page.locator("#addCollectionBook").click();
    await page.locator("#collectionSearchInput").fill("2");
    await page.locator("#collectionSearchForm button").click();
    try { await page.locator('[data-collect-result="2"]').click({timeout:5000}); }
    catch (error) { console.error("UI errors:",errors,"results:",await page.locator("#collectionAddContent").textContent()); throw error; }
    await page.locator("#followAllSeries").check();
    assert.equal(await page.locator("#followSeries").isChecked(),true);
    assert.equal(await page.locator("#followSeries").isDisabled(),true);
    assert.equal(await page.locator('#wishlistForm [type="submit"]').textContent(),"加入我的藏書");
    await page.locator('#wishlistForm [type="submit"]').click();
    await page.waitForFunction(()=>document.querySelectorAll(".book-card").length===2);
    await page.locator("#addCollectionBook").click();
    await page.locator("#createCustomBook").click();
    await page.locator('[name="title"]').fill("我的私人藏書");
    await page.locator('#customCollectionForm [type="submit"]').click();
    await page.locator('[data-id="-12"]').waitFor();
    assert.equal(await page.locator('[data-id="-12"] .title-cover').textContent(),"我的私人藏書");
    assert.equal(await page.locator('[data-id="-12"] [data-wishlist]').count(),0);
    await page.locator('[data-id="2"]').click();
    await page.waitForFunction(()=>document.querySelector('#wishlistForm [type="submit"]')?.textContent==="儲存藏書");
    assert.equal(await page.locator('#wishlistForm [type="submit"]').textContent(),"儲存藏書");
    await page.locator("#dialogClose").click();
    await page.setViewportSize({width:390,height:844});
    await page.locator("#addCollectionBook").click();
    assert.equal(await page.locator("#collectionAddDialog").evaluate(el=>el.getBoundingClientRect().width<=window.innerWidth),true);
    assert.deepEqual(errors,[]);
    console.log("UI smoke OK: heart cancellation keeps collection, catalog add, all-series checkbox, private book cover, labels, mobile dialog");
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
