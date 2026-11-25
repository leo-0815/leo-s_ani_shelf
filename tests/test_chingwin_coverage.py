import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.chingwin_coverage import (
    collect_ncl,
    evaluate_source,
    main,
    match_baseline_row,
    monthly_candidates,
    normalize_isbn13,
    parse_ncl_csv,
    read_retailer_samples,
)

FIXTURES = Path(__file__).parent / "fixtures"
class ChingwinCoverageTest(unittest.TestCase):
    def test_same_isbn_print_vs_ebook_not_merged(self):
        base_ebook = {
            "title": "測試作品(01)(電子書)",
            "isbn": "9786264499903",
            "edition_type": "digital",
            "media_type": "ebook",
            "volume_label": "1",
        }
        print_candidate = {
            "title": "測試作品(01)",
            "isbn": "9786264499903",
            "media": "print",
            "edition_type": "standard",
            "volume": "1",
            "title_key": "測試作品",
        }
        outcome, matched = match_baseline_row(base_ebook, [print_candidate])
        self.assertEqual(outcome, "miss")
        self.assertIsNone(matched)

    def test_special_vs_standard_not_merged(self):
        base_special = {
            "title": "測試作品(02)特裝版",
            "isbn": None,
            "edition_type": "special",
            "media_type": "manga",
            "volume_label": "2",
        }
        standard_candidate = {
            "title": "測試作品(02)",
            "isbn": "9786260000001",
            "media": "print",
            "edition_type": "standard",
            "volume": "2",
            "title_key": "測試作品",
        }
        outcome, matched = match_baseline_row(base_special, [standard_candidate])
        self.assertEqual(outcome, "miss")
        self.assertIsNone(matched)

    def test_ncl_csv_month_precision_and_publisher_filter(self):
        csv_text = (
            "\ufeff申請書名,作者,出版機構,版次,預訂出版日,ISBN,出版形式,裝訂方式,分級註記\n"
            "測試作品(03),作者A,青文,初版,2026-11-00,978-626-03-0377-8,紙本,平裝,限制級\n"
            "別家作品(01),作者B,東立,初版,2026-11-00,9786260000002,紙本,平裝,\n"
        )
        rows = parse_ncl_csv(csv_text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["planned_ym"], "2026-11")
        self.assertEqual(rows[0]["release_precision"], "month")
        self.assertEqual(rows[0]["isbn"], "9786260303778")
        self.assertEqual(rows[0]["media"], "print")

    def test_ncl_alternate_header_and_unknown_format(self):
        row, = parse_ncl_csv(
            "申請書名,出版機構簡稱,ISBN,裝訂方式,出版形式\n"
            "測試作品,青文,9786260303778,,單行本\n"
        )
        self.assertIsNone(row["media"])
        self.assertEqual(row["isbn"], "9786260303778")
        self.assertIsNone(normalize_isbn13("9786260303770"))

    def test_ncl_uses_only_official_urls_and_keeps_partial_results(self):
        load = Mock(return_value="申請書名,出版機構簡稱,ISBN\n測試,青文,9786260303778\n".encode())
        allowed = "https://isbn.ncl.edu.tw/example.csv"
        out = collect_ncl(csv_urls=[allowed, "https://isbn.ncl.edu.tw.evil.invalid/example.csv"], load_csv_bytes=load)
        load.assert_called_once_with(allowed)
        self.assertEqual(out["status"], "partial")
        self.assertEqual(len(out["records"]), 1)

    def test_ncl_reports_latest_listed_month_even_when_latest_download_fails(self):
        urls = [f"https://www.ncl.edu.tw/{i}.csv" for i in (8, 9)]
        meta = json.dumps({"result": {"distribution": [
            {"resourceDownloadUrl": urls[0], "resourceDescription": "115年8月份"},
            {"resourceDownloadUrl": urls[0], "resourceDescription": "115年8月份"},
            {"resourceDownloadUrl": urls[1], "resourceDescription": "115年9月份"},
        ]}})
        load = Mock(side_effect=[
            "申請書名,出版機構,ISBN\n測試,青文,9786260303778\n".encode(), OSError("unavailable"),
        ])
        result = collect_ncl(ncl_months=2, load_meta=lambda _: meta, load_csv_bytes=load)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["latest_listed_month"], "2026-09")
        self.assertEqual(result["latest_read_month"], "2026-08")

    def test_monthly_candidates_hold_conflicts_and_never_join_print_and_ebook(self):
        row = dict(isbn="9786260303778", media="print", title="測試(01)", title_key="測試",
                   volume="1", edition_type="standard", planned_ym="2026-10", dataset_month="2026-08",
                   resource_url="https://www.ncl.edu.tw/old.csv", rating_raw="限制級")
        latest = row | {"dataset_month": "2026-09", "planned_ym": "2026-11",
                        "resource_url": "https://www.ncl.edu.tw/new.csv"}
        ebook = row | {"media": "ebook", "edition_type": "digital"}
        unknown = row | {"media": None}
        review = monthly_candidates([row, latest, ebook, unknown])
        self.assertEqual(review["candidate_count"], 2)
        self.assertEqual(review["unresolved_rows"], 1)
        paper = next(book for book in review["candidates"] if book["media"] == "print")
        self.assertEqual(paper["planned_month"], "2026-11")
        self.assertNotIn("release_date", paper)
        self.assertNotIn("rating_raw", paper)
        self.assertEqual(monthly_candidates([row, row | {"volume": "2"}])["conflicting_groups"], 1)
        self.assertEqual(monthly_candidates([row, row | {"planned_ym": "2026-12"}])["candidate_count"], 0)

    def test_same_isbn_with_wrong_edition_is_not_a_hit(self):
        base = dict(title="測試作品(01)(特典版)", edition_type="bonus",
                    media_type="manga", isbn="9786264499903", volume_label="1")
        standard = dict(isbn=base["isbn"], media="print", edition_type="standard", volume="1")
        self.assertEqual(match_baseline_row(base, [standard])[0], "isbn_unverified")
        base["edition_type"] = "standard"
        out = evaluate_source([base], [standard, dict(standard)])
        self.assertEqual(out["coverage"]["unknown|standard"]["hit"], 1)

    def test_title_only_is_not_counted_as_isbn_hit(self):
        base = dict(title="測試作品(01)", edition_type="standard", media_type="manga")
        candidate = dict(media="print", edition_type="standard", volume="1", title_key="測試作品",
                         isbn="9786264499903")
        self.assertEqual(match_baseline_row(base, [candidate])[0], "title_only")

    def test_offline_comparison_never_opens_sources_or_promotes_listings(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            with patch("sys.argv", ["chingwin-coverage", "--baseline",
                                    str(FIXTURES / "chingwin_oct_nov_2026_sample.json"),
                                    "--retailer-samples", str(FIXTURES / "chingwin_retailer_manual_samples.json"),
                                    "--skip-ncl", "--out", str(out)]), patch(
                                        "app.chingwin_coverage.build_opener"
                                    ) as opener, patch("builtins.print"):
                main()
            opener.assert_not_called()
            data = json.loads(out.read_text(encoding="utf-8"))
            sources = data["sources"]
            self.assertFalse(data["actual_catalog_write"])
            self.assertFalse(sources["kingstone"]["automatic_access_verified"])
            self.assertEqual(sum(x["hit"] for x in sources["kingstone"]["coverage"].values()), 5)
            self.assertEqual(sources["kingstone"]["date_offset_days"]["min_days"], -20)
            self.assertEqual(sum(x["title_only"] for x in sources["sanmin"]["coverage"].values()), 3)
            self.assertEqual(sources["udn"]["coverage"]["2026-11|digital"]["title_only"], 1)

    def test_local_evidence_requires_the_retailer_origin_and_valid_identifiers(self):
        row = dict(title="測試(01)", publisher="青文", media="print", edition_type="standard",
                   isbn="9786264499903", source_url="https://www.kingstone.com.tw/basic/123/")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "samples.json"
            for invalid in (dict(source_url="https://www.kingstone.com.tw.attacker.invalid/basic/123/"),
                            dict(publisher="香港青文"), dict(isbn="4718016048263")):
                source.write_text(json.dumps({"kingstone": [row | invalid]}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_retailer_samples(source)

    def test_gtin_remains_separate_from_isbn_and_needs_same_title(self):
        base = dict(title="示範書盒", gtin="4718016048263", edition_type="bundle", media_type="manga")
        candidate = dict(gtin=base["gtin"], media="print", edition_type="bundle", title_key="示範書盒")
        self.assertEqual(match_baseline_row(base, [candidate])[0], "hit")
        self.assertEqual(match_baseline_row(base, [candidate | {"title_key": "另一套"}])[0], "isbn_unverified")

if __name__ == "__main__":
    unittest.main()
