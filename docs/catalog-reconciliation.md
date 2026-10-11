# PR47：既有雙來源整併與封面補齊

PR46 防止後續新增重複；PR47 提供明確執行的維護流程，沒有啟動時全表整併、背景 AI 或暖機。

## 流程

- 先預覽青文已存在的出書表／商品配對。完整書名、作者、卷數、媒體、版本或有效 ISBN 都沿用 PR46 的保守身份規則；不同官方商品 SKU 不自動合併。
- 每批至多 50 組，每組獨立交易。預覽帶公共書目雜湊 proof；來源資料已變更則拒絕該組，重新預覽。
- 商品書目 ID 保留，採商品封面、ISBN、連結與出版日，出書表可補完整作者。出版社短鎖與重新核對防止同時新增造成歧義。
- 訂選、藏書、推薦排除、日期歷史、同步 change feed、通知 ledger 及補查 checkpoint 都重新指向保留 ID。私人欄位不改寫；同使用者有衝突、重複藏書實體 ID、人工分級衝突或不認識的外鍵即保留兩筆，列入 blocked。
- 通知里程碑／新書事件 key 隨 ID 更新，重複 ledger 可折疊；既有分級待上傳保留。較完整 metadata 仍保護人工分級及官方 BL。
- 舊書目 ID 留下轉址，登入帳號的詳情／訂選／藏書操作與訪客舊 ID 批次刷新皆可使用。訪客的私人紀錄不送上伺服器，也不刪除瀏覽器原始紀錄。
- catalog_book_merges 保存 DB-only 前像，包括相關私人關聯。API 不回傳這些欄位；不要將此表匯出至公開 repo。交易失敗自動 rollback；已提交後需依備份與前像人工檢查復原，沒有未驗證的一鍵反向合併。

## 缺封面

既有雙來源整併會保留商品封面，正常商品列表增量也會補上對應原書目。JSON-LD 的字串、圖片物件、列表及相對網址統一成有效 http(s) URL，不下載或快取圖片。

尚未找到商品、商品尚未上架或官方本來沒圖者仍顯示文字封面；不靠相似書名套其他卷／特裝版的封面。本 PR 不會重爬全部歷史商品，也不能保證每本預定書都有封面。

## 本機同步

雲端 status 新增 source_aliases_version=1；受既有 catalog-sync token 保護的 aliases endpoint 僅輸出公共 canonical 書目與舊來源碼。公共同步 hash v1 不變。

本機 pull、push 及 manifest 修復上傳前先套別名：先更新商品公共書目，再整併本機對應的舊來源碼，保留本機私人資料。若本機私人資料有衝突，停止上傳，保留 checkpoint／outbox，不重新產生已刪除的舊碼。舊雲端沒有此能力則略過別名，不強制換協議。

cloud/deployment 與 main 本機版本架構不同，不能直接整份覆蓋。local-compat/ 保存對 main 的局部相容補丁與本機 alias adapter；共用 identity／merger 模組來自 app/。本次也已在本機主工作樹套用，不提交原有青文分級或其他尚未提交變更。

## 驗收

執行 python -m unittest discover -v 及 node scripts/test_guest_store.cjs。
設定 ANISHELF_TEST_LOCAL_MYSQL=1 可跑 tests.test_book_identity_mysql：只允許 localhost，全部是連線限定 temporary tables，包含 JSON 前像和實際整併 SQL，不寫正式資料。

維護工具只在本機 main 版本執行：python scripts/reconcile_catalog_sources.py 預設只預覽；
加 --apply 才逐批整併；最後執行既有 python anishelf.py catalog-sync pull 接收來源別名。
