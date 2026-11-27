import unittest

from scripts.chingwin_saved_product import parse_product


class SavedProductTests(unittest.TestCase):
    opening = '''<html><head>
    <meta property="og:title" content="名偵探柯南(108) - 青文出版">
    <meta name="Author" content="青文出版">
    </head><body>'''

    def test_only_used_product_spec_author(self):
        row = parse_product(self.opening + '''
            <div>產品編號：101018X108</div>
            <div>作家：<a>青山剛昌</a>　譯者：某人</div>
            <div>ISBN：978-626-030-609-0</div>
            <div>出版日期：2026/12/25</div>
            <div>內容簡介</div><div>作家：另一位作者</div>
            </body></html>''', "101018X108")
        self.assertEqual(row["title"], "名偵探柯南(108)")
        self.assertEqual(row["author"], "青山剛昌")
        self.assertEqual(row["isbn"], "9786260306090")
        self.assertEqual(row["published"], "2026-12-25")

    def test_never_borrows_author_from_description(self):
        with self.assertRaisesRegex(ValueError, "作家"):
            parse_product(self.opening + "產品編號：101018X108 定價 NT$200 作家：其他人", "101018X108")

    def test_wrong_edition(self):
        with self.assertRaisesRegex(ValueError, "商品編號"):
            parse_product(self.opening + "產品編號：101018X108A 作家：青山剛昌", "101018X108")

    def test_challenge(self):
        with self.assertRaisesRegex(ValueError, "驗證"):
            parse_product("<title>Just a moment...</title>", "101018X108")


if __name__ == "__main__":
    unittest.main()
