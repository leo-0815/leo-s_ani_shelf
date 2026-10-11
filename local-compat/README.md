# 本機 main 相容層

不要把 cloud/deployment 的 repository.py／catalog_sync.py 整份覆蓋到 main。

1. 在 main 用 git apply --check 檢查 main-source-identity.patch，再 git apply 套用。若已套用，可用 git apply --reverse --check 確認；不要重覆套用。
2. 從同版 cloud app/ 複製 book_identity.py 和 catalog_reconciliation.py 到 main app/，從本目錄複製 catalog_alias_sync.py 到 main app/。
3. 複製 scripts/reconcile_catalog_sources.py 到 main scripts/。此腳本只在本機版本使用。
4. 初始化新表後執行既有 catalog-sync pull。pull/push 會先套公共來源別名；私人衝突停止上傳，不覆蓋資料。

本機現有青文歷史年度／前後緩衝 checkpoint 與分級 runner 不要替换；這個补丁未包含使用者主工作樹其他未提交修改。正常雲端增量已有 PR46 的商品列表 refresh；本機原有來源程式可另行同步這段小範圍調整，不要覆蓋 BL 出書表保留功能。

不要把 DB-only 合併前像或 .env 放入 Git。
