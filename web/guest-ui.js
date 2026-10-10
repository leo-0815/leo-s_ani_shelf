/* UI for explicit browser persistence, backups and opt-in account migration. */
window.AniShelfGuestUi = (() => {
  const store = () => window.AniShelfGuest;
  const count = data => Object.keys(data.wishlist).length + Object.keys(data.collection).length + Object.keys(data.custom).length + data.follows.length;
  async function session(local) {
    const user = await apiRequest("/api/guest/start", {method:"POST",body:"{}"}, "guest-entry");
    return {...user, ...local.preferences};
  }
  async function enter(button) {
    button.disabled = true;
    try {
      const local = await store().read();
      await session(local); // Do not mark entered if issuance failed.
      await store().mutate(data => {data.entered = true;});
      history.replaceState(null,"","/#home");
      location.reload();
    } catch (error) {toast(error.message, true); button.disabled = false;}
  }
  async function google(event) {
    event.preventDefault();
    try {await store().mutate(data => {data.entered = false;});}
    catch (error) {
      if (state.user?.is_guest) {toast(error.message, true); return;}
      // Storage-disabled visitors may still use Google, without local import.
    }
    location.assign("/auth/google");
  }
  async function download() {
    const data = await store().read();
    saveFile(JSON.stringify(store().backup(data),null,2),"application/json","anishelf-guest-"+new Date().toISOString().slice(0,10)+".json");
    toast("訪客備份已下載；此檔案可能包含私人筆記，請妥善保管");
  }
  function saveFile(text,type,name) {
    const url = URL.createObjectURL(new Blob([text],{type}));
    const link = document.createElement("a"); link.href=url;
    link.download=name;
    document.body.append(link); link.click(); link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function exportFile(path) {
    const data=await store().read();
    if (path.startsWith("/api/export.csv")) {
      const cell=value=>'"'+String(value??"").replace(/^(\s*[=+@-])/u,"'$1").replaceAll('"','""')+'"';
      const rows=Object.entries(data.collection).map(([id,row])=>({...data.books[id],...row})).concat(Object.values(data.custom));
      const fields=['title','author','publisher_name','media_type','isbn','owned_format','purchased_at','paid_price','store_name','order_number','notes'];
      const csv='\ufeff'+[fields,...rows.map(row=>fields.map(key=>row[key]))].map(row=>row.map(cell).join(',')).join('\r\n');
      saveFile(csv,"text/csv;charset=utf-8","anishelf-guest-collection.csv");
      return;
    }
    const query=new URL(path,location.origin).searchParams;query.set('offset','0');
    const items=[];let more=true;
    while(more) {
      const page=await api('/api/upcoming?'+query);
      items.push(...page.items);more=page.offset+page.items.length<page.total;
      if(more && (!page.items.length || items.length>=4000))throw new Error('日曆書目過多，請縮短日期範圍');
      query.set('offset',String(items.length));
    }
    const esc=v=>String(v||'').replaceAll('\\','\\\\').replaceAll('\n','\\n').replaceAll(',','\\,').replaceAll(';','\\;').replaceAll('\r','');
    const stamp=new Date().toISOString().replaceAll('-','').replaceAll(':','').replace(/\.\d{3}/,'');
    const lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//AniShelf//Guest Shelf//ZH'];
    for(const book of items)if(book.release_precision==='day' && /^\d{4}-\d{2}-\d{2}$/.test(book.release_date||''))lines.push('BEGIN:VEVENT',`UID:guest-book-${book.id}@anishelf`,`DTSTAMP:${stamp}`,`DTSTART;VALUE=DATE:${book.release_date.replaceAll('-','')}`,`SUMMARY:${esc(book.title)}`,`DESCRIPTION:${esc(book.publisher_name+' · 預定出版，請以商品頁為準')}`,'END:VEVENT');
    lines.push('END:VCALENDAR');saveFile(lines.join('\r\n')+'\r\n','text/calendar','anishelf-guest-calendar.ics');
  }
  async function restore(file) {
    if (!file) return;
    if (file.size > 5*1024*1024) throw new Error("備份檔最多 5 MB");
    let raw;
    try {raw=JSON.parse(await file.text());} catch (_) {throw new Error("備份不是有效的 JSON 檔案");}
    const incoming=store().parseBackup(raw);
    if (!confirm(`備份含 ${count(incoming)} 筆私人記錄。只新增缺少項目，保留目前筆記及偏好，確定還原？`)) return;
    await store().mutate(data => store().merge(data,incoming));
    store().reset();
    toast("訪客備份已還原；Google 帳號資料未變更");
    if (state.user?.is_guest) {state.catalogPromise=null; await switchView(state.view,true);}
  }
  async function offer(force=false) {
    if (!state.user || state.user.is_guest) return;
    let local;
    try {local=await store().read();} catch (_) {return;}
    const owner=state.user.id;
    if (!count(local) || (!force && local.decisions[owner])) {
      if (force) toast("這個瀏覽器還沒有可匯入的訪客書架");
      return;
    }
    const dialog=$("#guestImportDialog"), content=$("#guestImportContent");
    if (dialog.open) return;
    content.innerHTML='<p class="eyebrow">YOUR BROWSER SHELF</p><h2 id="guestImportTitle">要把訪客書架帶到帳號嗎？</h2>'+
      `<p>此瀏覽器有 ${count(local)} 筆訂選、藏書、自建書與追蹤。按下查看摘要才會將這份資料（包含筆記）傳到你的登入帳號；再次確認後才儲存。</p>`+
      '<p>只新增缺少的項目，不覆蓋帳號既有筆記、購入資訊或追蹤；瀏覽器原始資料會保留。一次最多 1,000 筆。</p><p id="guestImportStatus" role="status"></p>'+
      '<div class="form-actions"><button class="primary" id="guestImportReview">查看匯入摘要</button><button class="secondary" id="guestImportLater">暫不匯入</button></div>';
    dialog.showModal();
    const later=async()=>{await store().mutate(data=>{data.decisions[owner]="later";});dialog.close();};
    $("#guestImportLater").addEventListener("click",()=>later().catch(error=>toast(error.message,true)));
    $("#guestImportReview").addEventListener("click",async()=>{
      const button=$("#guestImportReview"); button.disabled=true;
      try {
        if (state.user?.id!==owner || state.user.is_guest) throw new Error("登入帳號已變更，請重新開啟匯入");
        const data=store().payload(local);
        const preview=await api("/api/guest-import/preview",{method:"POST",body:JSON.stringify({data})});
        const s=preview.summary;
        $("#guestImportStatus").textContent=`將新增 ${s.new} 筆：訂選 ${s.wishlist}、藏書 ${s.collection}、自建 ${s.custom}、追蹤 ${s.follows}。保留既有 ${s.existing} 筆；無法對應 ${s.missing} 筆會略過，仍留在瀏覽器。`;
        button.textContent="確認匯入到目前帳號";
        const apply=button.cloneNode(true); button.replaceWith(apply); apply.disabled=false;
        let expired=false;
        apply.addEventListener("click",async()=>{
          if (expired) {dialog.close();await offer(true);return;}
          apply.disabled=true;
          try {
            if (state.user?.id!==owner || state.user.is_guest) throw new Error("登入帳號已變更，請重新預覽");
            const result=await api("/api/guest-import/apply",{method:"POST",body:JSON.stringify({data,token:preview.token})});
            try {await store().mutate(d=>{d.decisions[owner]="imported";});} catch (_) { /* Cloud commit is already successful; never retry it automatically. */ }
            dialog.close(); toast(`已匯入 ${result.summary.new} 筆；訪客原始書架仍保留`);
            state.catalogPromise=null; await switchView(state.view,true);
          } catch(error) {
            $("#guestImportStatus").textContent=error.message;apply.disabled=false;
            if (error.status===409) {expired=true;apply.textContent="重新查看匯入摘要";}
          }
        });
      } catch(error) {$("#guestImportStatus").textContent=error.message;button.disabled=false;}
    });
  }
  function init() {
    $("#guestLogin").addEventListener("click",event=>enter(event.currentTarget));
    $("#guestSwitch").addEventListener("click",event=>enter(event.currentTarget));
    [$("#googleLogin"),$("#guestGoogleLogin"),...$$(".guest-google")].forEach(link=>link.addEventListener("click",google));
    $("#guestBackup").addEventListener("click",()=>download().catch(error=>toast(error.message,true)));
    $("#guestRestore").addEventListener("click",()=>$("#guestBackupFile").click());
    $("#guestBackupFile").addEventListener("change",event=>{
      const file=event.target.files[0];event.target.value="";
      restore(file).catch(error=>toast(error.message,true));
    });
    $("#guestImport").addEventListener("click",()=>offer(true).catch(error=>toast(error.message,true)));
    $$('a[href^="/api/export.csv?collection="],a[href^="/api/calendar.ics"]').forEach(link=>link.addEventListener("click",event=>{
      if (!state.user?.is_guest) return;
      event.preventDefault();exportFile(link.getAttribute('href')).catch(error=>toast(error.message,true));
    }));
  }
  return {init,session,offer};
})();
