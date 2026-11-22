# 尖端出書表恢復（PR56）

- 先讀取官方 `https://event.spp.com.tw/event/publish.aspx`，核對上市日、漫畫／動漫、書號欄位及近期有效日期。
- 進度頁失效時，才從官網最新出版頁探索直接提供的 Google 試算表。僅使用當次官方頁面提供的文件 ID，最多兩份文件、每份近期五個月；移除已刪除試算表 ID 的固定備援。
- 登入頁、錯誤頁、空表、欄位不符及超出近期範圍的表不能算成功。
- 所有出書表路徑失敗仍保留既有 80 筆商品頁備援，但來源狀態明示出書表失效，不將商品備援誤報為全功能恢復。
- 官方出書表保留相同書號身份；稀疏欄位不清空既有作者、ISBN、封面、定價及分級，經確認的商品頁日期優先。
- 不變更 schema、私人書架、分級解析版本、每日排程時間或額度。

## 驗證

`python -m unittest tests.test_spp_schedule tests.test_sources tests.test_release_dates tests.test_repository -q`

包括官方進度表、新試算表探索、HTTP 410 後改讀新表、過期／錯誤頁拒絕、商品備援保留警告、商品日期及完整資料不被出書表覆寫。

正式環境使用 Scheduled catalog update 的 `test_publisher_only=spp`，只更新尖端、不寄送通知、不跑其他出版社或歷史全爬。
