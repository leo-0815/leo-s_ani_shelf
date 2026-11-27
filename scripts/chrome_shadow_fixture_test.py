"""Ad hoc browser/probe smoke test. Every request targets a loopback fixture."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

from app import publisher_shadow
from app.sources.chingwin import ChingWinSource

SCHEDULE = """<!doctype html><meta charset="utf-8">
<meta property="og:title" content="2026年12月預定出書表">
<main id="schedule"></main>
<script>
const editions = ["測試漫畫(05)", "測試漫畫(05)特別版"];
for (const title of editions) {
  const row = document.createElement("p");
  row.textContent = "類別：漫畫 書名：" + title +
                    " 作者：測試作者 書系：測試 定價：140 首刷附錄：無 電子書：無";
  document.querySelector("#schedule").appendChild(row);
}
</script>"""
BLOCKED_BODY = "<html><title>Fixture verification required</title><h1>驗證中</h1></html>"
RESPONSES = {
    "/about-news-detail/212": (200, {}, SCHEDULE),
    "/healthy-list": (200, {}, "<main>catalog fixture</main>"),
    "/plain-403": (403, {}, BLOCKED_BODY),
    "/marked-403": (403, {"cf-mitigated": "challenge"}, BLOCKED_BODY),
    "/marked-200": (200, {"cf-mitigated": "challenge"}, BLOCKED_BODY),
    "/unmarked-200": (200, {}, BLOCKED_BODY),
}


@contextmanager
def fixture_server():
    hits = Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits[self.path] += 1
            status, headers, body = RESPONSES.get(self.path, (404, {}, "Not found"))
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", hits
        finally:
            server.shutdown()
            thread.join()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def check_shadow(base, hits):
    for path, expected in (
        ("/plain-403", {"status": "access_blocked", "http": 403}),
        ("/marked-403", {"status": "challenge", "http": 403}),
        ("/marked-200", {"status": "challenge"}),
    ):
        fixtures = {
            "chingwin": (
                ("schedule", base + path, "catalog", "fixture"),
                ("detail", base + "/must-not-fetch", "unreachable", "fixture"),
            ),
            "other": (("listing", base + "/healthy-list", "catalog", "fixture"),),
        }
        with patch.object(publisher_shadow, "OFFICIAL", fixtures):
            result = publisher_shadow.run(pause=lambda _: None)
        require(result["sources"]["chingwin"] == {"schedule": expected}, f"Incorrect result for {path}")
        require(result["sources"]["other"]["listing"] == {"status": "sample_verified"}, "Other source did not run")
        require(hits[path] == 1 and hits["/must-not-fetch"] == 0, "Blocked source was requested again")
        print(f"- {path}: {expected['status']}; stopped publisher only")
    unknown = publisher_shadow.probe(base + "/unmarked-200", lambda html: "預定出書表" in html)
    require(unknown == {"status": "layout_or_evidence_unverified"}, "Unmarked page was treated as verified")
    print("- unmarked HTTP 200 without schedule contents: not verified")


def check_browser(base, chrome):
    url = base + "/about-news-detail/212"
    source = ChingWinSource()
    require(source._parse_article(url, publisher_shadow.fetch(url)) == [], "Fixture should require JS rendering")
    with TemporaryDirectory(prefix="anishelf-fixture-chrome-") as profile:
        result = subprocess.run(
            [chrome, "--headless", "--disable-background-networking", "--no-first-run",
             f"--user-data-dir={profile}", "--dump-dom", url],
            check=True, capture_output=True, timeout=25, encoding="utf-8",
        )
    rows = source._parse_article(url, result.stdout)
    require([(row.volume_label, row.edition_type) for row in rows] == [("05", "standard"), ("05", "special")],
            "Expected matching volume and distinct standard/special editions")
    require(all(row.release_date == date(2026, 12, 1) and row.release_precision == "month"
                and row.release_date_source == "schedule" for row in rows), "Expected month-only publisher evidence")
    print("- Chrome rendered 2 editions of volume 05; actual AniShelf parser preserved planned month (2026-12)")


def main():
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    require(chrome is not None, "Chrome not installed in the runner image")
    print("## Localhost-only Chrome and publisher probe test")
    with fixture_server() as (base, hits):
        check_shadow(base, hits)
        check_browser(base, chrome)
    print("- No publisher, production database, or notification credentials used")


if __name__ == "__main__":
    main()
