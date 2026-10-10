const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

(async () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
  const code = source.slice(source.indexOf("const apiInflight"), source.indexOf("function showLogin"));
  let count = 0, release, now = 0, fail = false;
  const ctx = vm.createContext({
    state: {user: {id: 1}, csrfToken: "csrf"},
    Date: {now: () => now},
    showLogin() {},
    fetch: async (url, options) => {
      count++;
      assert.equal(options.headers["Content-Type"], "application/json");
      if (fail) return {status: fail.status || 429, ok: false, headers: {get: () => String(fail.retry || 60)},
        json: async () => ({rate_limit_bucket: fail.bucket || "browse", retry_after: fail.retry || 60, read_protection: Boolean(fail.resource)})};
      await new Promise(resolve => release = resolve);
      return {status: 200, ok: true, json: async () => ({url})};
    },
  });
  vm.runInContext(code, ctx);
  const first = ctx.api("/api/books?q=x");
  const duplicate = ctx.api("/api/books?q=x");
  assert.equal(first, duplicate);
  assert.equal(count, 1);
  release(); await Promise.all([first, duplicate]);
  const fresh = ctx.api("/api/books?q=x");
  assert.equal(count, 2); release(); await fresh;
  ctx.state.contentMode='general';
  const oldMode=ctx.api('/api/books?q=mode');const oldRelease=release;
  ctx.state.contentMode='bl';
  const newMode=ctx.api('/api/books?q=mode');const newRelease=release;
  assert.notEqual(oldMode,newMode,'different visibility modes cannot reuse an in-flight response');
  oldRelease();newRelease();await Promise.all([oldMode,newMode]);
  ctx.$=()=>{throw Error('stale mode must not touch rendered statistics');};
  vm.runInContext(source.slice(source.indexOf('async function loadStats()'),source.indexOf('function selectablePublishers()')),ctx);
  const staleStats=ctx.loadStats();const statsRelease=release;
  ctx.state.contentMode='r18';statsRelease();await staleStats;
  const stalePublishers=ctx.loadPublishers();const publishersRelease=release;
  ctx.state.contentMode='all';publishersRelease();await stalePublishers;
  fail = true;
  await assert.rejects(ctx.api("/api/books"), /60 秒/);
  const blockedCount = count;
  await assert.rejects(ctx.api("/api/series?q=new"), /60 秒/);
  assert.equal(count, blockedCount, "cooldown must prevent network retries in same category");
  assert.equal(ctx.apiBucket("/api/preferences", "POST"), "write");
  assert.equal(ctx.apiBucket("/api/books/1/recommendations", "GET"), "recommendations");
  fail = false;
  ctx.state.user.id = 2;
  const otherUser = ctx.api("/api/books");
  release(); await otherUser;
  ctx.state.user.id = 1; now = 60000;
  const resumed = ctx.api("/api/books");
  release(); await resumed;
  fail = {status:503,bucket:'read-overload',retry:2,resource:true};
  await assert.rejects(ctx.api('/api/stats'), /2 秒/);
  const overloadedCount=count;
  await assert.rejects(ctx.api('/api/guest/books/2/recommendations'), /2 秒/);
  assert.equal(count,overloadedCount,'read protection spans public query categories');
  fail=false;
  const write=ctx.api('/api/preferences',{method:'POST',body:'{}'});release();await write;
  const metrics=ctx.api('/api/resource-guards');release();await metrics;
  now=62000;
  const recovered=ctx.api('/api/books');release();await recovered;
  fail={bucket:'read-budget',retry:30,resource:true};
  await assert.rejects(ctx.api('/api/books'),/30 秒/);
  const budgetCount=count;
  await assert.rejects(ctx.api('/api/recommendations'),/30 秒/);
  assert.equal(count,budgetCount);
  assert.match(source, /}, 6000\)/);
  assert.match(source, /if \(polling\) return/);
  console.log("API deduplication, cooldown, account isolation and polling tests OK");
})().catch(error => {console.error(error); process.exit(1)});
