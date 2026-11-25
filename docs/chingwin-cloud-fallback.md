# PR #63：青文雲端備援

第一階段：GitHub Actions 的 **Publisher source shadow check** 每家至多讀兩個公開頁，分開回報樣本通過、挑戰頁、限流、其他 HTTP 錯誤和版型或欄位未確認。不使用 TiDB、登入或通知；不改現行每日更新。通路探測只限人工確認允許自動取用後明確開啟；遇攔阻不重試或換身分。

這是樣本檢查，不是書目覆蓋率、同批更新或部署驗收。下一階段必須先比對青文 10、11 月出版表與通路的分版本覆蓋及上架延遲，並觀察 GitHub-hosted runner。可參考國圖月更開放資料補發現，不拿其日期冒充當日官方上市日期。沒有獲准 source 時保持唯讀。

正式寫入門檻：

- 官網維持首選；有挑戰頁停止該來源，切到已驗證、獲准的備援。寫入預設關閉。
- 同版匹配須核對出版社、卷、媒體、版本與識別碼。471 是 GTIN 不是 ISBN；同 ISBN 的紙本／電子書也不合併。保留舊書籍 ID、藏書與追蹤。含糊的同版先待確認。
- 通路出版日與官方商品頁日期、預購和出貨日期分欄。已確認的官方日期不覆蓋。僅接受能對到同版的明確分級／分類證據，BL 和 R18 分開，無標記不降級。
- 既有批次重跑不得增生重複。其他出版社不受青文來源停擺影響。不將「尚未驗證」或「零筆」顯示為新書更新成功。

## 2026-11-26：國圖唯讀比對

基準是青文 [10 月紙本](https://www.ching-win.com.tw/about-news-detail/208)、[10 月電子](https://www.ching-win.com.tw/about-news-detail/209)、[11 月紙本](https://www.ching-win.com.tw/about-news-detail/210)、[11 月電子](https://www.ching-win.com.tw/about-news-detail/211)公告抽出的 10 筆樣本，不是完整書單。樣本檔各列附官方來源；公告月份和商品頁出版日期分開。電子樣本為單話，未找到 ISBN 不代表市面上沒有該電子商品。

使用[國家圖書館「臺灣出版新書預告書訊」](https://data.gov.tw/dataset/6730)（政府資料開放授權條款第 1 版）[官方月檔](https://isbn.ncl.edu.tw/NEW_ISBNNet/main_ProcessMenuItems.php?Pact=ViewContent&Pfld=Ffile&Ptarget=418&Pval=418)，比對 2026 年 7–10 月已公開的 ISBN 申請資料；當時官方索引未提供 11 月檔。四檔共有 767 列標為青文，未跨檔去重。10 筆樣本中，5 筆一般版皆命中；3 筆特殊版有 2 筆同 ISBN 但版本未確認、1 筆只有 GTIN 未找到；2 筆電子單話未找到。這不是全體命中率或上架時差，也不代表國圖可取代官網日更。國圖的預訂日只保留到年月。

重跑時用 `python -m app.chingwin_coverage --baseline tests/fixtures/chingwin_oct_nov_2026_sample.json --out chingwin-ncl-sample-report.json`，加上重複的 `--ncl-csv` 指定要比對的官方月檔。前述 7–10 月檔在官方索引分別對應 `1787213979.csv`、`1790071160.csv`、`1793352819.csv`、`1795412644.csv`。舊、新 CSV 的出版者欄位分別可能為「出版機構」與「出版機構簡稱」。

通路比對只讀人工確認的本機資料，詳見[通路可行性評估](chingwin-retailer-feasibility.md)。沒有自動取用或寫入書目；即使有公開商品頁，也不拿出貨日補成出版日。
