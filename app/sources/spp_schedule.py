from __future__ import annotations

import csv
import html
import io
import re
from datetime import date
from .common import fetch_html, parse_tables

SHOP = "https://www.spp.com.tw/v2/official/SalePageCategory/536787"
PUBLISH = "https://event.spp.com.tw/event/publish.aspx"


def sheet_tabs(markup: str) -> list[tuple[int, int, str]]:
    tabs = []
    for name, gid in re.findall(
        r'items\.push\(\{name:\s*"([^"]+)".*?gid:\s*"(\d+)"', markup
    ):
        match = re.fullmatch(r"(\d{3,4})(\d{2})\s*[（(]動漫[）)]", name.strip())
        if match:
            year, month = int(match[1]), int(match[2])
            if year < 1911:
                year += 1911
            if 1 <= month <= 12:
                tabs.append((year, month, gid))
    return sorted(set(tabs), reverse=True)


def schedule_rows(markup: str, year: int) -> list[list[str]]:
    rows = list(csv.reader(io.StringIO(markup)))
    if not any(len(row) >= 4 and row[0].strip() == "上市日" for row in rows):
        raise RuntimeError("尖端官方出書表缺少上市日欄位")
    result = []
    for row in rows:
        if len(row) < 4:
            continue
        value = row[0].strip()
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", value)
        if match:
            value = date(year, int(match[1]), int(match[2])).strftime("%Y/%m/%d")
        if re.fullmatch(r"20\d{2}/\d{1,2}/\d{1,2}", value):
            result.append([value, row[1].strip(), row[2].strip(), row[3].strip()])
    return result


def _recent(records):
    today = date.today()
    current = today.year * 12 + today.month
    result = {}
    for record in records:
        day = record.release_date
        if (not day or not current - 2 <= day.year * 12 + day.month <= current + 2
                or not re.fullmatch(r"[A-Za-z0-9-]{5,30}", record.source_key)):
            continue
        previous = result.get(record.source_key)
        if not previous or day > previous.release_date:
            result[record.source_key] = record
    if not result:
        raise RuntimeError("尖端官方出書表沒有近期有效書目（拒絕空白或過期頁面）")
    return list(result.values())


def _collect_sheet(document, parser):
    base = f"https://docs.google.com/spreadsheets/d/{document}"
    tabs = sheet_tabs(fetch_html(base + "/htmlview", timeout=20, attempts=1))
    if not tabs:
        raise RuntimeError("尖端官方出書表沒有動漫分頁")
    today = date.today()
    current = today.year * 12 + today.month
    selected = [tab for tab in tabs if current - 2 <= tab[0] * 12 + tab[1] <= current + 2]
    if not selected:
        raise RuntimeError("尖端官方出書表缺少近期動漫分頁")
    records = {}
    for year, month, gid in selected:
        csv_url = base + f"/export?format=csv&gid={gid}"
        rows = schedule_rows(fetch_html(csv_url, timeout=20, attempts=1), year)
        # Reuse the publisher table parser, preserving book codes as stable identities.
        table = "<table>" + "".join(
            "<tr>" + "".join("<td>" + html.escape(cell) + "</td>" for cell in row) + "</tr>"
            for row in rows
        ) + "</table>"
        for record in parser(base + f"/edit#gid={gid}", table):
            old = records.get(record.source_key)
            if not old or record.release_date > old.release_date:
                records[record.source_key] = record
    return _recent(records.values())


def collect_official_schedule(parser):
    """Official live table first; only documents linked by the official shop next.

    Never reuse a deleted spreadsheet ID or accept a login/error/stale page as
    success. No recursive discovery, arbitrary hosts, or unbounded retries.
    """
    errors = []
    try:
        markup = fetch_html(PUBLISH, timeout=20, attempts=1)
        rows = parse_tables(markup)
        if not any(len(r) >= 4 and r[0] == "上市日" and
                   ("漫畫" in r[1] or "動漫" in r[1]) and
                   ("書號" in r[3] or "書碼" in r[3]) for r in rows):
            raise RuntimeError("尖端官方進度頁缺少動漫出書表欄位")
        return _recent(parser(PUBLISH, markup))
    except (RuntimeError, ValueError) as exc:
        errors.append(f"進度頁：{exc}")
    try:
        shop = html.unescape(fetch_html(SHOP, timeout=20, attempts=1)).replace('\\/', '/')
        documents = list(dict.fromkeys(re.findall(
            r"https?://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{10,100})(?:[/#?\"'])", shop)))
        if not documents:
            raise RuntimeError("官網未提供可用出書表連結")
        for document in documents[:2]:
            try:
                return _collect_sheet(document, parser)
            except (RuntimeError, ValueError) as exc:
                errors.append(f"官網試算表：{exc}")
    except (RuntimeError, ValueError) as exc:
        errors.append(f"官網探索：{exc}")
    raise RuntimeError("官方出書表不可用；" + "；".join(errors))

