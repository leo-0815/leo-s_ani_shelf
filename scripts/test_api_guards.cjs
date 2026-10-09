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
      if (fail) return {status: 429, ok: false, headers: {get: () => "60"},
        json: async () => ({rate_limit_bucket: "browse", retry_after: 60})};
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
  assert.match(source, /}, 6000\)/);
  assert.match(source, /if \(polling\) return/);
  console.log("API deduplication, cooldown, account isolation and polling tests OK");
})().catch(error => {console.error(error); process.exit(1)});
