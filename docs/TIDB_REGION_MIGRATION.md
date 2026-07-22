# TiDB 東京到新加坡搬移

這份流程只搬雲端 AniShelf 資料庫，不會修改本機 MySQL。東京 TiDB 在完成切換與觀察前必須保留。

## 安全設計

- 未加 `-Execute` 時只會讀取兩端資料表筆數。
- 密碼使用 PowerShell 隱藏輸入，且只存在於子程序環境變數。
- 來源與目標指向同一資料庫時會拒絕執行。
- 目標已有使用者資料時會拒絕第一次複製。
- `-ResetTarget` 必須同時輸入完全相符的目標主機名稱。
- 複製後逐表比較筆數與 SHA-256 內容摘要。

## 1. 只讀預演

在雲端工作樹執行：

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\migrate_tidb_region.ps1" `
  -TargetHost "<新加坡 TiDB host>" `
  -TargetUser "<新加坡 TiDB user>"
```

兩次密碼輸入不會顯示在畫面上。預演不得建立 schema 或寫入資料。

## 2. 第一次完整複製

確認預演的來源與目標正確後，在相同指令最後加上 `-Execute`。第一次複製會建立目標 `anishelf` schema，保留所有原始 ID，並在完成後執行完整驗證。

## 3. 最終同步

正式切換前先停止排程更新並避免修改訂選資料。因目標尚未正式使用，可以清空新加坡目標後重新完整複製：

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\migrate_tidb_region.ps1" `
  -TargetHost "<新加坡 TiDB host>" `
  -TargetUser "<新加坡 TiDB user>" `
  -Execute `
  -ResetTarget `
  -ConfirmTarget "<新加坡 TiDB host>"
```

只有在所有資料表均顯示 `OK` 後，才能更新 Render 與 GitHub Actions 的資料庫連線設定。
