"""Read-only official-page grading smoke checks; no covers or DB writes."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sources.common import fetch_html, polite_pause
from app.sources.spp import SppSource
from app.sources.tongli import TongLiSource

SAMPLES = [
    ("spp", "https://www.spp.com.tw/SalePage/Index/11865455"),
    ("spp", "https://www.spp.com.tw/SalePage/Index/10052058"),
    ("tongli", "https://www.tongli.com.tw/BooksDetail.aspx?Bd=TC0433003B"),
]


def main():
    for code, url in SAMPLES:
        try:
            markup = fetch_html(url, timeout=20, attempts=1)
            if code == "spp":
                record, _ = SppSource()._parse_detail(url, markup, enforce_cutoff=False)
            else:
                record = TongLiSource()._parse_detail(url, markup)
            print(json.dumps(dict(publisher=code, url=url, title=record.title,
                rating=record.content_rating, raw=record.rating_raw), ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps(dict(publisher=code, url=url, error=str(exc)), ensure_ascii=False), flush=True)
        polite_pause(1.5)


if __name__ == "__main__":
    main()

