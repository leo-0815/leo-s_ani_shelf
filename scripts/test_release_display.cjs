const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");
(async () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
  const code = source.slice(source.indexOf("function publicationStatus"), source.indexOf("function releaseLabel"));
  const node = {innerHTML:""};
  let retry, fail = false;
  const ctx = vm.createContext({
    Intl, Date,
    statusLabels:{scheduled:"預定出版", available:"已上市"},
    escapeHtml:s => String(s).replace(/[<>]/g, c => c === "<" ? "&lt;" : "&gt;"),
    $:selector=>selector === "#releaseCheckSummary" ? node : {addEventListener:(e,f)=>retry=f},
    api:async()=>{if(fail)throw new Error("internal");return {date:"2026-10-08",items:[{
      name:"<publisher>",attempted_today:4,daily_limit:20,confirmed_today:1,
      undated_today:1,failed_today:1,incomplete_today:1,last_attempt_at:"2026-10-08T00:00:00Z",last_confirmed_at:null}]}},
  });
  vm.runInContext(code, ctx);
  assert.equal(ctx.publicationStatus({release_display_status:"pending_confirmation"}), "已過預定日，待確認");
  assert.equal(ctx.publicationStatus({release_display_status:"pending_confirmation"},true), "待確認");
  assert.equal(ctx.publicationStatus({release_display_status:"date_confirmed"},true), "日期已確認");
  assert.match(ctx.checkedLabel("2026-10-07T16:00:00Z"), /2026\/10\/8/);
  assert.match(ctx.dateProvenance({release_date_source:"product",release_checked_at_utc:"2026-10-07T16:00:00Z"}), /商品頁明確日期/);
  assert.match(ctx.dateProvenance({}), /尚未確認/);
  await ctx.loadReleaseChecks();
  assert.match(node.innerHTML,/4\/20/);
  assert.match(node.innerHTML,/&lt;publisher&gt;/);
  assert.doesNotMatch(node.innerHTML,/<publisher>/);
  fail=true;
  await ctx.loadReleaseChecks();
  assert.match(node.innerHTML,/重試回查摘要/);
  assert.doesNotMatch(node.innerHTML,/internal/);
  assert.equal(typeof retry,"function");
  fail=false;
  await retry();
  assert.match(node.innerHTML,/4\/20/);
  assert.match(source,/publicationStatus\(book, true\)/);
  assert.match(source,/dateProvenance\(book\)/);
  console.log("Release confirmation UI tests OK");
})().catch(e=>{console.error(e);process.exit(1)});
