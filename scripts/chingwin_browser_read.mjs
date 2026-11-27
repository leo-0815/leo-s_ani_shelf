// One read-only observation with Chrome. Never follow redirects, retry, or
// execute a challenge. Only the three specified top-level documents may load.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const root = 'https://www.ching-win.com.tw';
const announcement = `${root}/about-news/`;
const schedule = `${root}/about-news-detail/212`;
const product = `${root}/product-detail/101018X108`;
const cli = process.argv[2] || process.env.CHROME_BIN || 'google-chrome';
const tempRoot = resolve(tmpdir());
const profile = mkdtempSync(join(tempRoot, 'anishelf-browser-read-'));
let child;
let socket;
let stage = 'Chrome 啟動';
let active;
let launchFailed = false;
let exited = false;
const pending = new Map();
let serial = 0;

function send(method, params = {}) {
  return new Promise((ok, bad) => {
    const id = ++serial;
    const timer = setTimeout(() => {
      pending.delete(id);
      bad(new Error(`${method} 逾時`));
    }, 11000);
    pending.set(id, (error, result) => {
      clearTimeout(timer);
      if (error) bad(new Error(`${method} 失敗`));
      else ok(result);
    });
    try {
      socket.send(JSON.stringify({ id, method, params }));
    } catch {
      pending.get(id)(true);
      pending.delete(id);
    }
  });
}

async function intercept(p) {
  const id = { requestId: p.requestId };
  const isResponse = p.responseStatusCode !== undefined || p.responseErrorReason !== undefined;
  const block = () => send('Fetch.failRequest', { ...id, errorReason: 'BlockedByClient' });
  const matching = active && p.resourceType === 'Document' && p.request.method === 'GET'
    && p.request.url === active.url && p.frameId === active.frameId;
  if (!matching) return block();
  if (!isResponse) {
    if (active.started) return block();
    active.started = true;
    return send('Fetch.continueRequest', id);
  }
  const headers = p.responseHeaders || [];
  const mitigated = headers.some(h => h.name.toLowerCase() === 'cf-mitigated'
    && /challenge/i.test(h.value));
  const html = headers.some(h => h.name.toLowerCase() === 'content-type'
    && /\btext\/html\b/i.test(h.value));
  active.status = p.responseStatusCode;
  if (mitigated || active.status !== 200 || !html) {
    active.stop = mitigated ? 'Cloudflare 驗證' : (active.status ? `HTTP ${active.status}` : '網路請求失敗');
    if (active.status === 200 && !html && !mitigated) active.stop = 'HTTP 200，但不是 HTML';
    return block();
  }
  const body = await send('Fetch.getResponseBody', id);
  const htmlText = body.base64Encoded ? Buffer.from(body.body, 'base64').toString() : body.body;
  if (/<title[^>]*>\s*(?:just a moment|attention required|請稍候)|\/cdn-cgi\/challenge-platform|\bcf-chl-\b/i.test(htmlText)) {
    active.stop = '驗證頁';
    return block();
  }
  await send('Fetch.continueResponse', id);
}

async function readPage(label, url, expression, mainFrameId) {
  stage = label;
  active = { url, frameId: mainFrameId };
  const page = await send('Page.navigate', { url, frameId: mainFrameId });
  for (let attempt = 0; attempt < 45; attempt++) {
    if (active.stop) throw new Error(active.stop);
    if (page.errorText) throw new Error('Chrome 導覽失敗');
    try {
      const result = await send('Runtime.evaluate', {
        returnByValue: true,
        expression: `location.href === ${JSON.stringify(url)} && document.readyState !== 'loading' ? (${expression})() : null`,
      });
      if (result.exceptionDetails) throw new Error('DOM 讀取失敗');
      if (result.result?.value != null) {
        console.log(`${label}：HTTP ${active.status} ${JSON.stringify(result.result.value)}`);
        return result.result.value;
      }
    } catch (error) {
      if (error.message !== 'Runtime.evaluate 失敗') throw error;
    }
    await delay(200);
  }
  throw new Error(active.stop || '未能讀取頁面');
}

function exactLink(url) {
  return `() => { const anchor = [...document.querySelectorAll('a[href]')].find(a => a.href === ${JSON.stringify(url)} && a.innerText.trim()); return {found: !!anchor, label: anchor?.innerText.trim() || null}; }`;
}

function bookDetails() {
  const visible = document.body.innerText;
  const title = [...document.querySelectorAll('h1,h2,h3,h4,.product-title')]
    .map(e => e.innerText.trim()).find(s => /名偵探柯南\s*[（(]\s*108\s*[）)]/.test(s))
    || visible.match(/^.*名偵探柯南\s*[（(]\s*108\s*[）)].*$/m)?.[0]?.trim();
  const idAt = visible.search(/產品編號\s*[:：]\s*101018X108\b/);
  const specs = idAt >= 0 ? visible.slice(idAt, idAt + 1200) : '';
  const author = specs.match(/作家\s*[:：][\t ]*([^\r\n]+?)(?=[\t ]+(?:繪者|譯者|集數|包裝|級別|開本|出版日期|ISBN)\s*[:：]|[\r\n]|$)/)?.[1].trim();
  const isbn = specs.match(/ISBN\s*[:：]\s*(\d{13})\b/)?.[1];
  const published = specs.match(/出版日期\s*[:：]\s*(\d{4}[-/]\d{2}[-/]\d{2})\b/)?.[1];
  return { productId: idAt >= 0 ? '101018X108' : null, title: title || null, author: author || null,
    isbn: isbn || null, published: published || null };
}

try {
  if (typeof WebSocket !== 'function') throw new Error('需要 Node.js 22 以上');
  child = spawn(cli, ['--headless', '--incognito', '--disable-background-networking', '--no-first-run',
    '--no-default-browser-check', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'],
  { stdio: 'ignore', windowsHide: true });
  child.on('error', () => { launchFailed = true; });
  child.on('exit', () => { exited = true; });
  const portFile = join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 50 && !existsSync(portFile) && !launchFailed && !exited; i++) await delay(200);
  if (!existsSync(portFile)) throw new Error('無法啟動本機 Chrome DevTools');
  const port = Number(readFileSync(portFile, 'utf8').split(/\r?\n/)[0]);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('DevTools 連接埠無效');
  // This Fetch request is local discovery only; Chrome itself navigates all publisher URLs.
  const res = await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(3000) });
  const target = (await res.json()).find(t => t.type === 'page' && t.url === 'about:blank');
  if (!target?.webSocketDebuggerUrl) throw new Error('找不到新的 Chrome 分頁');
  const ws = new URL(target.webSocketDebuggerUrl);
  if (ws.origin !== `ws://127.0.0.1:${port}`) throw new Error('DevTools 不是本機端點');
  socket = new WebSocket(ws);
  await new Promise((ok, bad) => {
    const timer = setTimeout(() => bad(new Error('DevTools WebSocket 逾時')), 3000);
    socket.addEventListener('open', () => { clearTimeout(timer); ok(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); bad(new Error('DevTools WebSocket 不可用')); }, { once: true });
  });
  socket.addEventListener('message', ({ data }) => {
    const m = JSON.parse(data);
    if (m.id && pending.has(m.id)) {
      pending.get(m.id)(m.error, m.result);
      pending.delete(m.id);
    }
    if (m.method === 'Fetch.requestPaused') {
      intercept(m.params).catch(async () => {
        if (active && m.params.request.url === active.url) active.stop ||= 'Chrome 攔截時無法確認安全的 HTML 回應';
        await send('Fetch.failRequest', { requestId: m.params.requestId, errorReason: 'BlockedByClient' }).catch(() => {});
      });
    }
  });
  await send('Page.enable');
  const mainFrame = (await send('Page.getFrameTree')).frameTree.frame.id;
  await send('Fetch.enable', { patterns: [
    { urlPattern: '*', requestStage: 'Request' },
    { urlPattern: '*', resourceType: 'Document', requestStage: 'Response' },
  ] });
  if (!(await readPage('公告', announcement, exactLink(schedule), mainFrame)).found) throw new Error('找不到指定出書表連結');
  if (!(await readPage('出書表', schedule, exactLink(product), mainFrame)).found) throw new Error('找不到指定商品連結');
  const preview = await readPage('單本商品頁', product, bookDetails.toString(), mainFrame);
  stage = '書目預覽';
  if (!preview.productId || !preview.title || !preview.author) throw new Error('未在商品規格讀到書名與「作家」');
  console.log(`書目預覽（未入庫）：${JSON.stringify(preview)}`);
} catch (error) {
  const status = active?.status ? `（HTTP ${active.status}）` : '';
  console.error(`停止於 ${stage}${status}：${active?.stop || error.message}`);
  process.exitCode = 1;
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    // It is safe to send a close frame without waiting for Chrome's reply.
    socket.send(JSON.stringify({ id: ++serial, method: 'Browser.close' }));
    socket.close();
  }
  for (let i = 0; i < 15 && child?.pid && !exited; i++) await delay(100);
  if (child?.pid && !exited) child.kill();
  child?.unref();
  if (dirname(resolve(profile)) === tempRoot && basename(profile).startsWith('anishelf-browser-read-')) {
    try { rmSync(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }); } catch { /* Chrome may still be unwinding */ }
  }
}
