/* Guest personal state stays in IndexedDB. Only public book/series references
   are sent for catalog refresh; notes/orders are sent only on confirmed import. */
window.AniShelfGuest = (() => {
  const empty = () => ({version:1, entered:false, books:{}, wishlist:{}, collection:{}, custom:{}, follows:[], dismissed:[], decisions:{}, preferences:{general_audience:true,content_mode:"general"}, nextId:1});
  function contentPreferences(raw = {}) {
    raw = raw ?? {};
    const mode = raw.content_mode ?? (raw.general_audience === false ? "all" : "general");
    if (!["general","bl","r18","all"].includes(mode)) throw new Error("無效的內容顯示模式");
    return {content_mode:mode, general_audience:mode === "general"};
  }
  let dbPromise, refreshPromise, refreshGeneration = 0, refreshedAt = 0, visible = new Set();
  const publicCache = new Map();
  const clone = value => JSON.parse(JSON.stringify(value));
  const seriesKey = book => JSON.stringify([book.publisher_code || book.publisher, book.series_key, book.media_type]);
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  function text(value, max, label) {
    if (value == null) return "";
    if (typeof value !== "string" || value.length > max) throw new Error(`${label}格式或長度錯誤`);
    return value;
  }
  function date(value) {
    const v = text(value, 10, "日期");
    if (v && (!/^\d{4}-\d{2}-\d{2}$/.test(v) || !Number.isFinite(Date.parse(v + "T00:00:00Z")) || new Date(v + "T00:00:00Z").toISOString().slice(0,10) !== v)) throw new Error("日期格式錯誤");
    return v;
  }
  function ownership(row) {
    const format = row.owned_format || "paper";
    const price = row.paid_price == null || row.paid_price === "" ? null : Number(row.paid_price);
    if (!["paper","digital","both"].includes(format) || (price !== null && (!Number.isInteger(price) || price < 0 || price > 999999999))) throw new Error("收藏格式或價格錯誤");
    return {owned_format:format, purchased_at:date(row.purchased_at), paid_price:price,
      notes:text(row.notes,5000,"備註"), store_name:text(row.store_name,200,"店家"), order_number:text(row.order_number,200,"訂單編號")};
  }
  function customFields(row) {
    const title = text(row.title,500,"書名").trim(), media = row.media_type || "unknown";
    if (!title || !["manga","novel","unknown"].includes(media)) throw new Error("請填寫書名及有效類型");
    return {...ownership(row), title, media_type:media, author:text(row.author,500,"作者"), publisher_name:text(row.publisher_name,200,"出版社"), isbn:text(row.isbn,30,"ISBN"), edition_type:text(row.edition_type,100,"版本"), release_date:date(row.release_date)};
  }
  function validate(raw) {
    if (!object(raw) || raw.version !== 1) throw new Error("不支援的訪客備份版本");
    const data = empty();
    let count = 0;
    for (const name of ["books","wishlist","collection","custom"]) {
      if (!object(raw[name])) throw new Error("備份格式錯誤");
      for (const [key,row] of Object.entries(raw[name])) {
        if (!/^\d+$/.test(key) || !Number.isSafeInteger(Number(key)) || Number(key) <= 0 || !object(row)) throw new Error("備份書目識別碼錯誤");
        if (++count > 20000) throw new Error("備份資料過多");
        if (name === "books") {
          if (row.id !== Number(key) || typeof row.title !== "string" || row.title.length > 500) throw new Error("書目資料錯誤");
          // A snapshot contains public metadata only, never another account's fields.
          const fields = "id title normalized_title series_title series_key volume_label edition_type media_type content_rating bl_category author isbn cover_url list_price release_date release_precision release_status release_date_source release_checked_at release_checked_at_utc release_display_status first_seen_at updated_at source_url publisher_code publisher_name".split(" ");
          const book = {};
          for (const field of fields) {
            const value = row[field];
            if (value != null && !["string","number","boolean"].includes(typeof value)) throw new Error("書目欄位格式錯誤");
            if (typeof value === "string" && value.length > 2000) throw new Error("書目欄位過長");
            if (value !== undefined) book[field] = value;
          }
          data.books[key] = book;
        } else if (name === "custom") data.custom[key] = {id:Number(key), ...customFields(row)};
        else if (name === "collection") {
          if (!Number.isSafeInteger(row.id) || row.id <= 0) throw new Error("藏書識別碼錯誤");
          data.collection[key] = {id:row.id,...ownership(row)};
        } else {
          if (!["wanted","preordered","paused"].includes(row.state) || !Number.isInteger(row.priority ?? 0) || (row.priority ?? 0) < 0 || (row.priority ?? 0) > 3) throw new Error("訂選狀態錯誤");
          data.wishlist[key] = {...ownership(row), state:row.state, priority:row.priority || 0};
        }
      }
    }
    if (!Array.isArray(raw.follows) || raw.follows.length > 100 || !Array.isArray(raw.dismissed) || raw.dismissed.length > 5000) throw new Error("追蹤資料過多或格式錯誤");
    data.follows = raw.follows.map(row => {
      if (!object(row) || !["future","all"].includes(row.scope) || !["manga","novel","unknown","mixed"].includes(row.media_type)) throw new Error("系列追蹤格式錯誤");
      const publisher = text(row.publisher,64,"出版社"), key = text(row.series_key,190,"系列"), title = text(row.series_title,500,"系列");
      if (!publisher || !key || !title || !Array.isArray(row.seen) || row.seen.length > 20000 || row.seen.some(id=>!Number.isSafeInteger(id) || id <= 0)) throw new Error("系列追蹤格式錯誤");
      return {publisher,series_key:key,series_title:title,media_type:row.media_type,scope:row.scope,seen:[...new Set(row.seen)]};
    });
    if (raw.dismissed.some(id=>!Number.isSafeInteger(id) || id <= 0)) throw new Error("推薦資料格式錯誤");
    data.dismissed = [...new Set(raw.dismissed)];
    data.preferences = contentPreferences(raw.preferences);
    data.nextId = Math.max(1, ...Object.keys(data.custom).map(Number), ...Object.values(data.collection).map(row=>row.id)) + 1;
    data.entered = raw.entered === true;
    if (object(raw.decisions)) for (const [key,value] of Object.entries(raw.decisions)) if (/^\d+$/.test(key) && ["later","imported"].includes(value)) data.decisions[key] = value;
    return data;
  }
  function database() {
    if (!dbPromise) dbPromise = new Promise((resolve,reject) => {
      if (!window.indexedDB) return reject(new Error("此瀏覽器無法保存訪客書架，請改用 Google 登入"));
      const request = indexedDB.open("anishelf-guest-v1",1);
      request.onupgradeneeded = () => request.result.createObjectStore("state");
      request.onsuccess = () => {request.result.onversionchange=()=>request.result.close(); resolve(request.result);};
      request.onerror = () => {dbPromise=null; reject(new Error("訪客書架儲存空間無法開啟，請使用 Google 登入或檢查瀏覽器設定"));};
      request.onblocked = () => reject(new Error("請先關閉其他 AniShelf 分頁，再開啟訪客書架"));
    });
    return dbPromise;
  }
  async function read() {
    const db = await database();
    return new Promise((resolve,reject) => {
      const request = db.transaction("state","readonly").objectStore("state").get("shelf");
      request.onsuccess = () => {
        try {
          const data = request.result || empty();
          data.preferences = contentPreferences(data.preferences);
          resolve(data);
        } catch (error) { reject(error); }
      };
      request.onerror = () => reject(new Error("無法讀取訪客書架"));
    });
  }
  async function mutate(change) {
    const db = await database();
    return new Promise((resolve,reject) => {
      const tx = db.transaction("state","readwrite"), store = tx.objectStore("state");
      let result, error;
      const request = store.get("shelf");
      request.onsuccess = () => {
        try {const data=request.result || empty(); result=change(data); store.put(validate(data),"shelf");}
        catch (failure) {error=failure; tx.abort();}
      };
      tx.oncomplete = () => resolve(result);
      tx.onabort = tx.onerror = () => reject(error || new Error("訪客資料未儲存成功；可能是空間不足，請先備份或改用 Google 登入"));
    });
  }
  function reset() {refreshGeneration++; publicCache.clear(); refreshedAt=0; visible=new Set();}
  async function network(path, transport, options = {}) {
    const key = path + (options.body || "");
    if (publicCache.has(key) && publicCache.get(key).until > Date.now()) return clone(publicCache.get(key).data);
    let result;
    try {result = await transport(path, options);}
    catch (error) {
      if (!error.guestExpired) throw error;
      await transport("/api/guest/start", {method:"POST",body:"{}"});
      result = await transport(path, options); // Exactly one recovery, no retry loop.
    }
    if (publicCache.size > 150) publicCache.clear();
    publicCache.set(key,{until:Date.now()+30000,data:clone(result)});
    return result;
  }
  async function publicRead(path, transport, data, options) {
    const [base,query=""] = path.split("?"), params=new URLSearchParams(query);
    params.set("content_mode",contentPreferences(data.preferences).content_mode);
    return network(base.replace("/api/","/api/guest/")+"?"+params,transport,options);
  }
  async function refresh(transport, force=false) {
    if (refreshPromise) {
      await refreshPromise;
      return refresh(transport, force);
    }
    if (!force && Date.now()-refreshedAt < 30000) return;
    const current = refreshGeneration;
    refreshPromise = (async () => {
      const data=await read(), books={}, currentVisible=new Set();
      const ids=[...new Set([...Object.keys(data.wishlist),...Object.keys(data.collection)].map(Number))];
      for (let offset=0;offset<ids.length;offset+=50) {
        const rows=await publicRead("/api/book-batch?ids="+ids.slice(offset,offset+50).join(","),transport,data);
        if (current !== refreshGeneration) return;
        rows.items.forEach(book=>{books[book.id]=book; currentVisible.add(book.id);});
      }
      for (let offset=0;offset<data.follows.length;offset+=20) {
        const series=data.follows.slice(offset,offset+20); let after=0, pages=0;
        while (true) {
          const rows=await network("/api/guest/resolve",transport,{method:"POST",body:JSON.stringify({series,after,content_mode:contentPreferences(data.preferences).content_mode})});
          if (current !== refreshGeneration) return;
          rows.items.forEach(book=>{books[book.id]=book; currentVisible.add(book.id);});
          if (!rows.has_more) break;
          if (++pages>=20 || rows.next_after_id<=after) throw new Error("系列書目較多，請稍後再更新訪客追蹤");
          after=rows.next_after_id;
        }
      }
      const committed = await mutate(latest=>{
        if (current !== refreshGeneration) return false;
        Object.assign(latest.books,books);
        for (const follow of latest.follows) {
          const seen=new Set(follow.seen);
          for (const book of Object.values(books)) {
            if (seriesKey(book)!==seriesKey(follow) || book.release_status==="cancelled" || (follow.scope==="future" && book.release_status!=="scheduled") || seen.has(book.id)) continue;
            seen.add(book.id);
            if (!latest.collection[book.id] && !latest.wishlist[book.id]) latest.wishlist[book.id]={...ownership({}),state:"wanted",priority:0};
          }
          follow.seen=[...seen];
        }
        return true;
      });
      if (committed && current === refreshGeneration) {visible=currentVisible; refreshedAt=Date.now();}
    })().finally(()=>refreshPromise=null);
    return refreshPromise;
  }
  function decorate(book,data) {
    const b={...book}, wish=data.wishlist[b.id], owned=data.collection[b.id];
    const follow=data.follows.find(row=>seriesKey(row)===seriesKey(b));
    Object.assign(b,{wishlist_state:wish?.state || null,is_owned:Boolean(owned),collection_id:owned?.id || null,collection:owned || null,series_follow_scope:follow?.scope || "",series_following:Boolean(follow)});
    if (wish) Object.assign(b,{wishlist_notes:wish.notes,wishlist_priority:wish.priority,wishlist_store:wish.store_name,wishlist_order_number:wish.order_number,wishlist_paid_price:wish.paid_price,wishlist_format:wish.owned_format,wishlist_purchased_at:wish.purchased_at});
    return b;
  }
  function privateBooks(data, collection=false) {
    const records=collection ? data.collection : data.wishlist;
    const books=Object.keys(records).map(Number).filter(id=>visible.has(id) && data.books[id]).map(id=>decorate(data.books[id],data));
    if (collection) for (const row of Object.values(data.custom)) books.push({...row,id:-row.id,collection_id:row.id,is_custom:true,is_owned:true,collection:row,release_status:row.release_date?"available":"unknown",release_precision:row.release_date?"day":"unknown",publisher_code:"",series_title:"",series_key:"",wishlist_state:null});
    return books;
  }
  function filterBooks(items,params) {
    const q=(params.get("q") || "").normalize("NFKC").toLowerCase().split(/\s+/).filter(Boolean);
    let rows=items.filter(book=>q.every(term=>[book.title,book.author,book.isbn,book.series_title].some(v=>String(v||"").normalize("NFKC").toLowerCase().includes(term))));
    const publishers=params.get("publishers") || params.get("publisher");
    if (publishers) rows=rows.filter(book=>publishers.split(",").includes(book.publisher_code));
    for (const [key,field] of [["media_type","media_type"],["edition","edition_type"],["wishlist_state","wishlist_state"]]) if (params.get(key)) rows=rows.filter(book=>book[field]===params.get(key));
    if (params.get("status")) rows=rows.filter(book=>params.get("status")==="scheduled_undated" ? book.release_status==="scheduled" && !book.release_date : book.release_status===params.get("status"));
    if (params.get("owned_format")) rows=rows.filter(book=>book.collection?.owned_format===params.get("owned_format"));
    if (params.get("date_from")) rows=rows.filter(book=>book.release_date && book.release_date>=params.get("date_from"));
    if (params.get("date_to")) rows=rows.filter(book=>book.release_date && book.release_date<=params.get("date_to"));
    const sort=params.get("sort");
    rows.sort((a,b)=>sort==="title_asc" ? a.title.localeCompare(b.title,"zh-TW") : sort==="purchased_desc" ? String(b.collection?.purchased_at||"").localeCompare(String(a.collection?.purchased_at||"")) : sort==="release_asc" ? String(a.release_date||"9999").localeCompare(String(b.release_date||"9999")) : String(b[sort==="added_desc"?"first_seen_at":sort==="updated_desc"?"updated_at":"release_date"]||"").localeCompare(String(a[sort==="added_desc"?"first_seen_at":sort==="updated_desc"?"updated_at":"release_date"]||"")));
    const offset=Number(params.get("offset")||0), limit=Math.min(200,Number(params.get("limit")||100));
    return {items:rows.slice(offset,offset+limit),total:rows.length,offset,limit};
  }
  async function follow(payload,transport) {
    const data=await read(), params=new URLSearchParams({publisher:payload.publisher,title:payload.series_title,media_type:payload.media_type});
    let series;
    const existing = data.follows.find(row=>row.publisher===payload.publisher && row.series_title===payload.series_title && row.media_type===payload.media_type);
    if (payload.following!==false || !existing) series=await publicRead("/api/series/detail?"+params,transport,data);
    else series={publisher_code:existing.publisher,...existing};
    const scope=payload.scope || "future";
    if (!["future","all"].includes(scope)) throw new Error("追蹤範圍錯誤");
    await mutate(latest=>{
      const key=seriesKey(series), index=latest.follows.findIndex(row=>seriesKey(row)===key);
      if (payload.following===false) {if(index>=0)latest.follows.splice(index,1); return;}
      const row={publisher:series.publisher_code,series_title:series.series_title,series_key:series.series_key,media_type:series.media_type,scope,seen:index>=0 && latest.follows[index].scope===scope ? latest.follows[index].seen : []};
      if(index>=0)latest.follows[index]=row; else latest.follows.push(row);
    });
    reset();
    try {await refresh(transport,true); return {ok:true};}
    catch (_) {return {ok:true,warning:"追蹤設定已保存；書目補齊暫時失敗，稍後重新開啟書架會續查"};}
  }
  async function api(path,options,transport) {
    const url=new URL(path,"https://guest.invalid"), p=url.pathname, params=url.searchParams;
    const method=options.method || "GET", payload=options.body ? JSON.parse(options.body) : {};
    let data=await read();
    if (method!=="GET") {
      if (p==="/api/preferences") {
        if (payload.content_mode == null && typeof payload.general_audience!=="boolean") throw new Error("偏好格式錯誤");
        const preferences=contentPreferences(payload);
        await mutate(d=>d.preferences=preferences); reset(); return preferences;
      }
      if (p==="/api/series/follow") return follow(payload,transport);
      if (p==="/api/recommendations/dismiss") {await mutate(d=>{if(!d.dismissed.includes(payload.book_id))d.dismissed.push(payload.book_id);}); return {ok:true};}
      if (p==="/api/recommendations/dismissals") {await mutate(d=>d.dismissed=[]); return {ok:true};}
      let match;
      if ((match=p.match(/^\/api\/collection\/custom(?:\/(\d+))?$/)) && method==="POST") {
        const fields=customFields(payload); let id;
        await mutate(d=>{id=match[1]?Number(match[1]):d.nextId++; if(match[1] && !d.custom[id])throw new Error("找不到自建藏書"); d.custom[id]={id,...fields};});
        return {ok:true,collection_id:id};
      }
      if ((match=p.match(/^\/api\/collection\/(\d+)$/)) && method==="DELETE") {
        const id=Number(match[1]); await mutate(d=>{delete d.custom[id]; for(const [key,row] of Object.entries(d.collection))if(row.id===id)delete d.collection[key];}); return {ok:true};
      }
      if ((match=p.match(/^\/api\/(wishlist|collection\/book)\/(\d+)$/))) {
        const id=Number(match[2]), collection=match[1]==="collection/book";
        if(method==="DELETE" && !collection) {await mutate(d=>{delete d.wishlist[id];}); return {ok:true};}
        const book=await publicRead("/api/books/"+id,transport,data), fields=ownership(payload);
        if (method!=="POST") throw new Error("不支援的訪客操作");
        await mutate(d=>{
          d.books[id]=book;
          if(collection || payload.state==="purchased") d.collection[id]={id:d.collection[id]?.id || d.nextId++,...fields};
          if(!collection) {
            const wishState=payload.state==="purchased" ? "wanted" : payload.state || "wanted";
            if(!["wanted","preordered","paused"].includes(wishState))throw new Error("訂選狀態錯誤");
            d.wishlist[id]={...fields,state:wishState,priority:Number(payload.priority||0)};
          }
        });
        visible.add(id); refreshedAt=0;
        if ("follow_series" in payload && book.series_title) {
          try {return await follow({publisher:book.publisher_code,series_title:book.series_title,media_type:book.media_type,following:payload.follow_series,scope:payload.follow_scope||"future"},transport);}
          catch (_) {return {ok:true,warning:"書籍已儲存；系列追蹤未更新，請稍後重試追蹤設定"};}
        }
        return {ok:true};
      }
      throw new Error("此功能需要 Google 登入；訪客不會啟用雲端通知或管理功能");
    }
    if(p==="/api/preferences")return data.preferences;
    if((/^\/api\/books\?/.test(path) && (params.get("wishlist")==="1" || params.get("collection")==="1")) || ["/api/home","/api/stats","/api/recommendations"].includes(p)) {await refresh(transport); data=await read();}
    if(p==="/api/books" && (params.get("wishlist")==="1" || params.get("collection")==="1"))return filterBooks(privateBooks(data,params.get("collection")==="1"),params);
    if(p==="/api/home") {
      const today=new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Taipei"}).format(new Date()), end=new Date(Date.parse(today+"T00:00:00Z")+7*86400000).toISOString().slice(0,10);
      const wishes=privateBooks(data).filter(b=>b.wishlist_state!=="paused" && b.release_precision==="day" && b.release_date>=today && b.release_date<=end && !["cancelled","delayed"].includes(b.release_status)).sort((a,b)=>a.release_date.localeCompare(b.release_date));
      return {counts:{wishlist:privateBooks(data).length,collection:privateBooks(data,true).length,followed_series:data.follows.length},items:wishes.slice(0,6),date:today};
    }
    if(p==="/api/stats") {
      const stats=await publicRead(path,transport,data), owned=privateBooks(data,true);
      return {...stats,wishlist:privateBooks(data).length,followed_series:data.follows.length,purchased:owned.length,purchased_paper:owned.filter(b=>["paper","both"].includes(b.collection.owned_format)).length,purchased_digital:owned.filter(b=>["digital","both"].includes(b.collection.owned_format)).length,purchased_series:new Set(owned.filter(b=>b.series_key).map(seriesKey)).size,purchased_spend:owned.reduce((total,b)=>total+Number(b.collection.paid_price||0),0)};
    }
    let match;
    if((match=p.match(/^\/api\/collection\/custom\/(\d+)$/))) {if(!data.custom[match[1]])throw new Error("找不到自建藏書"); return data.custom[match[1]];}
    if(p==="/api/recommendations") {
      const seeds=Object.keys(data.collection).map(Number).filter(id=>visible.has(id)).slice(0,3), rows=new Map();
      for(const id of seeds) {
        const related=await publicRead(`/api/books/${id}/recommendations?limit=20`,transport,data);
        related.items.forEach(book=>{if(!data.collection[book.id] && !data.dismissed.includes(book.id))rows.set(book.id,decorate(book,data));});
      }
      return {items:[...rows.values()].slice(0,60),series_prompts:[],purchased_count:privateBooks(data,true).length,followed_count:data.follows.length};
    }
    const result=await publicRead(path,transport,data);
    if (p==="/api/series") result.items=result.items.map(row=>({...row,is_following:data.follows.some(f=>seriesKey(f)===seriesKey(row))}));
    else if (p==="/api/series/detail") {
      while(result.has_more) {
        const next=new URLSearchParams(params); next.set("offset",result.next_offset);
        const page=await publicRead("/api/series/detail?"+next,transport,data);
        if(!page.items.length || page.next_offset<=result.next_offset)break;
        result.items.push(...page.items); result.has_more=page.has_more; result.next_offset=page.next_offset;
        if(result.items.length>4000)throw new Error("系列書目過多，請改用查詢頁分頁瀏覽");
      }
      result.items=result.items.map(book=>decorate(book,data));
    } else if(result.items) result.items=result.items.map(book=>decorate(book,data));
    else if(result.id) return decorate(result,data);
    return result;
  }
  function payload(data) {
    return {version:1,wishlist:Object.entries(data.wishlist).map(([id,row])=>({book_id:Number(id),...row})),collection:Object.entries(data.collection).map(([id,row])=>({book_id:Number(id),...row})),custom:Object.values(data.custom),follows:data.follows.map(({seen,...row})=>row)};
  }
  function backup(data) {return {format:"anishelf-guest",version:1,exported_at:new Date().toISOString(),data:clone(data)};}
  function parseBackup(raw) {
    if (!object(raw) || raw.format!=="anishelf-guest" || raw.version!==1)throw new Error("不是 AniShelf 訪客備份檔");
    return validate(raw.data);
  }
  function merge(current,incoming) {
    for(const [id,book] of Object.entries(incoming.books)) if(!current.books[id])current.books[id]=book;
    for(const [id,row] of Object.entries(incoming.wishlist))if(!current.wishlist[id])current.wishlist[id]=row;
    for(const [id,row] of Object.entries(incoming.collection))if(!current.collection[id])current.collection[id]={...row,id:current.nextId++};
    const identity=row=>JSON.stringify([row.title,row.author,row.publisher_name,row.isbn,row.edition_type,row.media_type]);
    const customs=new Set(Object.values(current.custom).map(identity));
    for(const row of Object.values(incoming.custom))if(!customs.has(identity(row))){const id=current.nextId++;current.custom[id]={...row,id};customs.add(identity(row));}
    const follows=new Set(current.follows.map(seriesKey));
    for(const row of incoming.follows)if(!follows.has(seriesKey(row))){current.follows.push(row);follows.add(seriesKey(row));}
    current.dismissed=[...new Set([...current.dismissed,...incoming.dismissed])];
    return current;
  }
  return {read,mutate,api,reset,payload,backup,parseBackup,merge,validate,filterBooks,decorate,privateBooks,empty};
})();
