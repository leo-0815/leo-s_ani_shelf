# 本機歷史爬取與雲端同步

## 執行方式

雙擊專案根目錄的 `歷史資料更新.bat`：

1. 執行 30 分鐘。
2. 執行 1 小時。
3. 執行到目前處理中的年度區間完成。
4. 持續逐年往前執行，直到按 `Ctrl+C`。

時間限制只會在一個批次寫入成功並提交 checkpoint 後停止。若在抓取途中手動中止，已
完成的批次仍保留；未完成批次下次會重新抓取，不會跳過。

## 每次執行順序

1. 使用 `sync_hash v1` 比較並拉回雲端較新的書目。
2. 將本機較完整的非空欄位安全補回雲端。
3. 從 `source_backfill_progress` 的年度 checkpoint 繼續歷史爬取。
4. 上傳本機爬蟲產生的 catalog changes。
5. 再次確認本機與雲端 manifest 一致。

同步 API 不接受使用者、session、OAuth、願望清單、系列追蹤與通知欄位。`cloud_pull`
產生的本機事件不會回傳雲端，避免無限迴圈。已確認上傳的本機事件會只保留最近 500 個
ID 範圍，控制資料庫容量。

## Checkpoint 緩衝

- 青文頁碼會從已保存位置往前回退 2 頁。
- 尖端與東立 sitemap 會往前回退 1 個批次。
- 已存在的 `source_key` 會去重，因此重疊只增加少量檢查，不會複製書目。

這個緩衝可涵蓋出版社把新商品插入前方、導致舊頁碼向後移動的情況。

## 出版社範圍

- 青文、尖端、東立：使用年度窗口逐年擴充。
- 台灣角川、台灣東販：既有完整目錄及日常增量更新。
- 長鴻：依目前需求排除歷史擴充。

## 診斷命令

```powershell
python anishelf.py catalog-sync compare
python anishelf.py catalog-sync pull
python anishelf.py catalog-sync push
python anishelf.py catalog-sync reconcile
```

`compare` 是唯讀操作。其他命令只操作 catalog 書目資料。

## 歷史來源涵蓋範圍

歷史更新工作依序涵蓋青文、台灣角川、台灣東販、尖端與東立。角川從 checkpoint
往前重讀兩頁，東販往前重讀一頁，以承受出版社在清單前方插入商品造成的頁碼位移。
若角川或東販已經完成過全目錄回填，會直接沿用既有完成狀態，不會為每個年份重複
下載同一份完整目錄。
