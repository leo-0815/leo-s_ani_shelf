from __future__ import annotations

import csv
import io
import re
from datetime import date
from .common import fetch_html

SHOP = "https://www.spp.com.tw/v2/official/SalePageCategory/536787"
SHEET_ID = "1f1UKq7cHJuudWLZdlv7tWL8pFshYlsw7_HNKAmKbjks"


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


def collect_official_schedule(parser):
    # Discover the official document from the shop instead of relying on a stale event host.
    shop = fetch_html(SHOP, timeout=30)
    match = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", shop)
    document = match[1] if match else SHEET_ID
    base = f"https://docs.google.com/spreadsheets/d/{document}"
    tabs = sheet_tabs(fetch_html(base + "/htmlview", timeout=30))
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
        rows = schedule_rows(fetch_html(csv_url, timeout=30), year)
        # Reuse the publisher table parser, preserving book codes as stable identities.
        import html
        table = "<table>" + "".join(
            "<tr>" + "".join("<td>" + html.escape(cell) + "</td>" for cell in row) + "</tr>"
            for row in rows
        ) + "</table>"
        for record in parser(base + f"/edit#gid={gid}", table):
            records[record.source_key] = record
    if not records:
        raise RuntimeError("尖端官方出書表沒有解析到近期書目")
    return list(records.values())

