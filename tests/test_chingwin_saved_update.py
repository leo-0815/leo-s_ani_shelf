from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.chingwin_saved_update import check_catalog_conflicts, main, parse_batch


DAY = date(2026, 11, 28)
SKU = "101018X108"
HTML = f'''<html><head>
<meta property="og:title" content="名偵探柯南(108) - 青文出版">
<meta property="og:url" content="https://www.ching-win.com.tw/product-detail/{SKU}">
</head><body><div>產品編號：{SKU}</div>
<div>作家：青山剛昌</div><div>ISBN：9786260306090</div>
<div>出版日期：2026-12-25</div><div>級別：普</div><div>定價：140</div></body></html>'''


class SavedUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.inbox = Path(temp.name)
        self.manga = self.inbox / DAY.isoformat() / "manga"
        self.manga.mkdir(parents=True)

    def put(self, html=HTML, sku=SKU, folder=None):
        (folder or self.manga).joinpath(f"{sku}.html").write_text(html, encoding="utf-8")

    def test_preview_uses_only_local_products(self):
        self.put()
        with patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network")):
            [book] = parse_batch(self.inbox, DAY)
        self.assertEqual((book.source_key, book.isbn, book.media_type), (SKU, "9786260306090", "manga"))
        self.assertEqual((book.release_date, book.release_checked_at), (date(2026, 12, 25), datetime(2026, 11, 27, 16)))
        self.assertEqual(book.content_rating, "general")

    def test_old_batch_does_not_satisfy_new_day_and_no_db(self):
        self.put()
        self.assertEqual(parse_batch(self.inbox, date(2026, 11, 29)), [])
        with patch("sys.argv", ["import", "--inbox", str(self.inbox), "--date", str(DAY)]), \
                patch("app.chingwin_saved_update.apply_records") as apply:
            main()
        apply.assert_not_called()

    def test_challenge_and_invalid_isbn_fail_validation(self):
        self.put("<html><title>Just a moment...</title></html>")
        with self.assertRaisesRegex(ValueError, "驗證"):
            parse_batch(self.inbox, DAY)
        self.put(HTML.replace("9786260306090", "9786260306091"))
        with self.assertRaisesRegex(ValueError, "ISBN"):
            parse_batch(self.inbox, DAY)

    def test_batch_rejects_duplicate_sku(self):
        self.put()
        novel = self.manga.with_name("novel")
        novel.mkdir()
        self.put(folder=novel)
        with self.assertRaisesRegex(ValueError, "重複"):
            parse_batch(self.inbox, DAY)

    def test_catalog_cannot_reassign_existing_isbn(self):
        self.put()
        books = parse_batch(self.inbox, DAY)
        rows = [{"source_key": f"{SKU}A", "source_url": f"https://www.ching-win.com.tw/product-detail/{SKU}A",
                 "isbn": "9786260306090", "media_type": "manga"}]
        with self.assertRaisesRegex(ValueError, "另一個正式商品"):
            check_catalog_conflicts(books, rows)
