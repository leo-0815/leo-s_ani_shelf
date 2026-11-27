"""Branch-only comparison: offline daily simulation and bounded public reads."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict
from datetime import date
import io
import json
import re
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app import crawler, scheduled
from app.book_identity import find_chingwin_match
from app.catalog_sync import book_record_from_sync
from app.repository import _crawler_write_data
from app.sources.chingwin import ChingWinSource

PUBLISHER = ChingWinSource.list_url
ARTICLE = "https://www.ching-win.com.tw/about-news-detail/212"
PRODUCT = "https://www.ching-win.com.tw/product-detail/101018X108"
FRIEND = "https://site.darwin0416.org/library/visitor/catalog"
FIXTURE_SKUS = ("SIM05", "SIM05A")
FIXTURE_TITLES = ("測試漫畫(05)", "測試漫畫(05)特別版")
FIXTURE_DATES = ("2026-12-25", "2026-12-26")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def synthetic_pages():
    pages = {PUBLISHER: f'<a href="{ARTICLE}">青文出版社2026年12月預定出書表</a>'}
    pages[ARTICLE] = '<meta property="og:title" content="2026年12月預定出書表">' + "".join(
        f'<p>類別：漫畫 書名：{title} 作者：測試作者 書系：少年 定價：150 首刷附錄：無 電子書：無</p>'
        for title in FIXTURE_TITLES
    )
    catalog = []
    for sku, title, release in zip(FIXTURE_SKUS, FIXTURE_TITLES, FIXTURE_DATES):
        for data in ({"@type": "Book", "name": title, "author": {"name": "測試作者"}},
                     {"@type": "Product", "name": title, "sku": sku, "offers": {"price": 150}}):
            catalog.append(f'<script type="application/ld+json">{json.dumps(data)}</script>')
        pages[f"https://www.ching-win.com.tw/product-detail/{sku}"] = (
            f"<p>產品編號：{sku} 級別：普 出版日期：{release} 定價：150</p><p>預購到貨：2027-01-25</p>"
        )
    pages[ChingWinSource.category_urls[0][1] + "?page=1"] = "".join(catalog)
    pages[ChingWinSource.category_urls[1][1] + "?page=1"] = "<main>empty novel category fixture</main>"
    return pages


def check_collection():
    pages = synthetic_pages()
    fetched = []

    def fixture_get(url, **kwargs):
        fetched.append(url)
        require(url in pages, "Unexpected URL in simulation (no requests are sent)")
        return pages[url]

    with patch("app.sources.chingwin.fetch_html", side_effect=fixture_get), patch(
        "app.sources.chingwin.polite_pause"
    ), patch("app.sources.chingwin.one_year_cutoff", return_value=date(2026, 1, 1)):
        source = ChingWinSource()
        outgoing = source.collect()
    require(not source.errors and len(outgoing) == 4 and len(fetched) == 6,
            "Expected 2 schedule rows + 2 detailed rows in 6 fixture requests")
    announcements = [book for book in outgoing if book.release_date_source == "schedule"]
    products = [book for book in outgoing if book.release_date_source == "product"]
    require(len(announcements) == len(products) == 2, "Schedule/detail evidence was mixed")
    cursor = Mock()
    existing = [(book.prepared() | {"id": i}) for i, book in enumerate(announcements, 1)]
    cursor.fetchall.return_value = existing
    for i, (product, schedule) in enumerate(zip(products, announcements), 1):
        blob = json.loads(json.dumps(asdict(product), default=lambda value: value.isoformat()))
        from_wire = book_record_from_sync(blob)
        require(from_wire.content_rating == "general" and from_wire.release_precision == "day",
                "Explicit SKU grade/day lost at the sync validation boundary")
        require(str(from_wire.release_date) == FIXTURE_DATES[i - 1], "Arrival date was used as publication date")
        matched = find_chingwin_match(cursor, 1, from_wire.prepared())
        require(matched is not None and matched["id"] == i, "Product did not match its own edition")
        delayed = _crawler_write_data(from_wire.prepared(), schedule.prepared())
        require(delayed["release_date"] == from_wire.release_date and delayed["source_url"] == from_wire.source_url
                and delayed["content_rating"] == "general", "A schedule row erased newer confirmed facts")
    print("- Simulated supply: 6 reads, 2 planned rows, 2 product rows; identity matcher keeps editions separate")
    print("- Product publication day and explicit age grade survive schema validation and replay of an older schedule")


def check_blocked_daily():
    stages = []
    settings = SimpleNamespace(public_url="https://example.test", discord_webhook_url="", notification_lead_days=())
    peer = SimpleNamespace(code="spp", name="fixture peer", errors=[], skipped_count=0,
                           collect=Mock(return_value=[]))
    blocked = ChingWinSource()

    def blocked_fetch(url, **kwargs):
        require(url == PUBLISHER, "Collector kept reading after the blocked list")
        raise RuntimeError("Fixture HTTP 403")

    job = {}

    def finish(job_id, status, totals, message):
        job.update(id=job_id, status=status, **totals, message=message)

    def run_sources(codes):
        require("chingwin" in codes and "spp" in codes, "Scheduled source list changed")
        crawler._run_job(99, "scheduled", "incremental", ("chingwin", "spp"))
        return 99

    def stage(name):
        def mark(*args, **kwargs):
            stages.append(name)
            return {}
        return mark

    with ExitStack() as stack:
        for name in ("_start_job", "_mark_source", "get_sync_state", "known_source_keys",
                     "save_sync_state", "max_source_release_date", "_update_job_progress"):
            value = {} if name == "get_sync_state" else set() if name == "known_source_keys" else None
            stack.enter_context(patch.object(crawler, name, return_value=value))
        stage_refresh = stack.enter_context(patch.object(crawler, "_refresh_known_releases"))
        upsert = stack.enter_context(patch.object(crawler, "upsert_book"))
        stack.enter_context(patch.object(crawler, "SOURCES", {"chingwin": lambda: blocked, "spp": lambda: peer}))
        stack.enter_context(patch.object(crawler, "_finish_job", side_effect=finish))
        stack.enter_context(patch("app.recent_recheck.attach_failure_recorder"))
        fetch = stack.enter_context(patch("app.sources.chingwin.fetch_html", side_effect=blocked_fetch))
        for name, value in (("ensure_schema", None), ("get_settings", settings)):
            stack.enter_context(patch.object(scheduled, name, return_value=value))
        stack.enter_context(patch.object(scheduled, "run_incremental_sources", side_effect=run_sources))
        stack.enter_context(patch.object(scheduled, "get_job", side_effect=lambda _: job))
        for name in ("refresh_recent_ratings", "run_recent_rechecks", "deliver_email_notifications"):
            stack.enter_context(patch.object(scheduled, name, side_effect=stage(name)))
        discord = stack.enter_context(patch.object(scheduled, "deliver_notifications",
                                                  side_effect=stage("deliver_notifications")))
        with redirect_stdout(io.StringIO()):
            try:
                scheduled.main()
            except SystemExit as exc:
                require(exc.code == 1 and job["status"] == "partial", "Blocked source was not reported as partial")
            else:
                raise AssertionError("Blocked fixture unexpectedly counted as successful")
        upsert.assert_not_called()
        fetch.assert_called_once_with(PUBLISHER)
        peer.collect.assert_called_once()
        require(discord.call_args.kwargs["extra_events"][0].event_type == "crawl_status",
                "No partial-failure alert was prepared")
        require(stage_refresh.call_args_list[0].args[0] is blocked, "Existing date-stage behavior changed")
        require(stages == ["refresh_recent_ratings", "run_recent_rechecks", "deliver_email_notifications",
                           "deliver_notifications"], "Stage wiring changed")
    print("- Simulated 403: actual daily entry returned partial, peer proceeded, no catalog write was attempted")
    print("- Existing gap reproduced: date refresh is still invoked after source failure; rating and recent stages still run")
    print("- Storage, other publisher responses and notification delivery were stubbed; no production writes")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_read(url, markers):
    """One plain HTTPS request. Stop on any denial, redirect or missing evidence."""
    request = Request(url, headers={"User-Agent": "AniShelf/0.1 (one-time public catalog verification)"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=12) as response:
            if response.headers.get("cf-mitigated", "").lower() == "challenge":
                return {"http": response.status, "state": "challenge"}
            raw = response.read(2_500_001)
            if len(raw) > 2_500_000:
                return {"http": response.status, "state": "size_limit"}
            html = raw.decode("utf-8", errors="replace")
            if re.search(r"<title>\s*(?:Just a moment|請稍候)", html, re.I) or "cf-chl-" in html:
                return {"http": response.status, "state": "challenge"}
            verified = all(re.search(marker, html, re.I) for marker in markers)
            return {"http": response.status, "state": "public_fields_verified" if verified else "evidence_not_verified"}
    except HTTPError as exc:
        state = ("challenge" if exc.headers.get("cf-mitigated", "").lower() == "challenge" else
                 "redirect_not_followed" if 300 <= exc.code < 400 else
                 "access_blocked" if exc.code in {401, 403, 429} else "http_error")
        return {"http": exc.code, "state": state}
    except (URLError, OSError):
        return {"state": "transport_error"}


def check_public():
    print("## Live public reading from GitHub (no browser challenge handling)")
    steps = [
        ("Chingwin news", PUBLISHER, (r"/about-news-detail/212", r"預定出書表")),
        ("Chingwin schedule", ARTICLE, (r"預定出書表", r"101018X108\b")),
        ("Chingwin detail", PRODUCT, (r"101018X108\b", r"ISBN", r"出版日期")),
    ]
    for label, url, markers in steps:
        out = public_read(url, markers)
        print(f"- {label}: {json.dumps(out)}")
        if out["state"] != "public_fields_verified":
            print("- Stop this source at first unverified step; no retries or alternate clients.")
            break
    visitor = public_read(FRIEND, (r"官方書目", r"/library/visitor/books/\d+"))
    print(f"- Friend public catalog: {json.dumps(visitor)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("simulate", "public"))
    if parser.parse_args().mode == "simulate":
        print("## Daily update simulation (no network or production storage)")
        check_collection()
        check_blocked_daily()
    else:
        check_public()


if __name__ == "__main__":
    main()
