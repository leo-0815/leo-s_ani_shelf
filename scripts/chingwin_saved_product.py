import argparse
import base64
from datetime import date
from html import unescape
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = "https://www.ching-win.com.tw"
DEFAULT_PRODUCT = "101018X108"
PRODUCT_ID = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
PRODUCT_LABEL = re.compile(r"產品編號\s*[:：]\s*([A-Za-z0-9_-]+)")
CHALLENGE = re.compile(
    r"<title\b[^>]*>\s*(?:just a moment|attention required|access denied|請稍候)"
    r"|/cdn-cgi/challenge-platform|\bcf-chl-", re.I,
)
SECTION_END = re.compile(r"(?:定價|優惠價|內容簡介|商品介紹|推薦商品|相關商品)")
SPEC_LABELS = (
    "產品編號|作家|作者|繪者|譯者|集數|包裝|級別|開本|尺寸|出版日期|頁數|ISBN"
)


class ProductPage(HTMLParser):
    BLOCKS = {"br", "div", "p", "li", "tr", "td", "th", "h1", "h2", "h3", "dt", "dd"}
    SKIP = {"script", "style", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.meta = {}
        self._ignored = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._ignored.append(tag)
        if self._ignored:
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "meta":
            fields = dict(attrs)
            name = fields.get("property") or fields.get("name")
            if name and fields.get("content"):
                self.meta.setdefault(name.lower(), fields["content"])

    def handle_endtag(self, tag):
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._ignored:
            self.parts.append(data)

    @property
    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def parse_product(markup: str, expected: str) -> dict:
    if CHALLENGE.search(markup):
        raise ValueError("這是驗證或拒絕頁，不是商品頁")
    page = ProductPage()
    page.feed(markup)
    page.close()
    source = f"{ROOT}/product-detail/{expected}"
    canonical = page.meta.get("og:url")
    if canonical:
        url = urlsplit(canonical)
        if (url.scheme != "https" or url.netloc not in {"www.ching-win.com.tw", "ching-win.com.tw"}
                or url.path.rstrip("/") != f"/product-detail/{expected}"):
            raise ValueError("HTML 的商品網址與指定商品不符")
    text = page.text
    sku = PRODUCT_LABEL.search(text)
    if not sku or sku[1] != expected:
        raise ValueError(f"找不到指定的商品編號 {expected}")
    specs = SECTION_END.split(text[sku.end():sku.end() + 1600], maxsplit=1)[0]

    author_field = re.search(r"作家\s*[:：][ \t]*", specs)
    author = None
    if author_field:
        remainder = specs[author_field.end():].lstrip()
        author_line = re.split(rf"\n|(?:{SPEC_LABELS})\s*[:：]", remainder, maxsplit=1)[0]
        if author_line.strip() not in {"", "-", "－", "—"}:
            author = author_line.strip()

    html_title = re.search(r"<title\b[^>]*>(.*?)</title\s*>", markup, re.I | re.S)
    raw_title = page.meta.get("og:title") or (unescape(html_title[1]) if html_title else "")
    title = re.sub(r"\s*[-–—|｜]\s*青文出版.*$", "", " ".join(raw_title.split()))
    if not title or not author:
        raise ValueError("商品編號相符，但未讀到書名或商品規格的「作家」")

    isbn_match = re.search(r"ISBN\s*[:：]\s*(97[89](?:[ -]?\d){10})(?![ -]?\d)", specs, re.I)
    date_match = re.search(r"出版日期\s*[:：]\s*(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", specs)
    published = None
    if date_match:
        try:
            published = date(*map(int, date_match.groups())).isoformat()
        except ValueError:
            pass
    return {
        "productId": expected,
        "title": title,
        "author": author,
        "isbn": re.sub(r"\D", "", isbn_match[1]) if isbn_match else None,
        "published": published,
        "source": source,
    }


def get_chrome_path() -> str:
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
        chrome, _ = winreg.QueryValueEx(key, None)
        return chrome


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cdp_eval_js(ws_url: str, expression: str) -> str:
    """純標準庫實作極簡 WebSocket 連線，透過 Chrome DevTools Protocol 執行 JS 取得 DOM"""
    parsed = urlsplit(ws_url)
    host, port = parsed.hostname, parsed.port
    sock = socket.create_connection((host, port), timeout=10)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode("utf-8"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket 握手失敗")
            resp += chunk

        # 傳送 CDP 指令 (Client 端發送必須帶 4-byte Mask)
        payload = json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True}
        }).encode("utf-8")

        mask = os.urandom(4)
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x81, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
        sock.sendall(header + mask + masked_payload)

        # 讀取回傳 Frame
        def recv_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("CDP 連線意外中斷")
                buf += chunk
            return buf

        first_two = recv_exact(2)
        payload_len = first_two[1] & 0x7F
        if payload_len == 126:
            payload_len = struct.unpack("!H", recv_exact(2))[0]
        elif payload_len == 127:
            payload_len = struct.unpack("!Q", recv_exact(8))[0]
        data = json.loads(recv_exact(payload_len).decode("utf-8"))
        return data.get("result", {}).get("result", {}).get("value", "")
    finally:
        sock.close()


def fetch_html_via_chrome(url: str, profile: Path, timeout_sec: int = 60) -> str:
    """啟動正常 Chrome 並透過本機除錯埠自動抓取渲染後的完整 HTML"""
    chrome = get_chrome_path()
    port = get_free_port()
    proc = subprocess.Popen([
        chrome,
        "--no-first-run",
        "--new-window",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        url,
    ])
    deadline = time.monotonic() + timeout_sec
    try:
        ws_url = None
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as res:
                    targets = json.loads(res.read().decode("utf-8"))
                for t in targets:
                    if t.get("type") == "page" and "ching-win.com.tw" in t.get("url", ""):
                        ws_url = t.get("webSocketDebuggerUrl")
                        break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if not ws_url:
            raise ValueError("無法連接 Chrome 取得分頁資訊")

        # 輪詢直到 Cloudflare 驗證通過且商品編號出現在 DOM 中
        while time.monotonic() < deadline:
            ready = cdp_eval_js(ws_url, "document.readyState")
            html = cdp_eval_js(ws_url, "document.documentElement.outerHTML")
            if ready == "complete" and html and not CHALLENGE.search(html) and "產品編號" in html:
                return html
            time.sleep(1.0)

        raise ValueError("等待網頁載入或 Cloudflare 驗證逾時（若跳出驗證框請手動勾選）")
    finally:
        proc.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-id", default=DEFAULT_PRODUCT)
    parser.add_argument("--html", type=Path, help="直接解析已儲存的 HTML，不開 Chrome")
    parser.add_argument("--save-html", type=Path, help="（選用）將自動抓到的 HTML 另存成檔案")
    args = parser.parse_args()

    if not PRODUCT_ID.fullmatch(args.product_id):
        parser.error("商品編號格式不正確")
    try:
        if args.html:
            markup = args.html.read_text(encoding="utf-8-sig")
        else:
            work = Path(tempfile.mkdtemp(prefix="chingwin-preview-"))
            target_url = f"{ROOT}/product-detail/{args.product_id}"
            markup = fetch_html_via_chrome(target_url, work / "profile", timeout_sec=90)
            if args.save_html:
                args.save_html.parent.mkdir(parents=True, exist_ok=True)
                args.save_html.write_text(markup, encoding="utf-8")

        print(json.dumps(parse_product(markup, args.product_id), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"停止：{exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "已停止等待。\n")


if __name__ == "__main__":
    main()