# AniShelf B 計畫：GitHub 自動化與雲端資料庫

狀態：保留供後續執行，現階段先完成出版社資料庫建置。

## 目標架構

- GitHub 私有 Repository：程式碼、測試、部署與排程設定。
- GitHub Actions：出版社增量更新、通知、測試與備份。
- TiDB Cloud Starter：正式書籍資料、願望清單、系列追蹤與通知紀錄。
- Render Web Service：執行 AniShelf 網頁與 API。
- Discord Webhook：即將上市、今日上市、日期異動與新刊通知。
- Email API：每日或每週電子報。

## 核心原則

1. 正式資料不存放於 Git Repository，也不依賴使用者電腦上的 MySQL。
2. Render 只提供網站與 API，不負責定時爬蟲。
3. GitHub Actions 定時執行爬蟲和通知，即使本機關機仍可運作。
4. 密碼、資料庫連線與 Webhook URL 全部放在 GitHub/Render Secrets。
5. 雲端切換後以 TiDB 為唯一正式資料來源，本機 MySQL 僅供開發或備份。

## 預定階段

### 第一階段：雲端相容

- 支援 TiDB TLS 連線。
- 支援雲端環境變數與 Render `PORT`。
- 將「網站啟動自動更新」改為雲端可關閉。
- 增加 health check、migration 與備份指令。

### 第二階段：帳號與資料隔離

- 新增 `users`。
- `wishlist_items` 與 `followed_series` 加入 `user_id`。
- 建立管理者登入、Session、CSRF 防護與寫入 API 權限。
- 可選擇公開書庫、私人願望清單。

### 第三階段：通知

- 新增通知偏好、通知事件和發送紀錄。
- 支援上市前 7/3/1 天、今日上市、日期異動與追蹤系列新刊。
- 使用事件唯一鍵避免重複通知。
- 優先完成 Discord，之後增加 Email 摘要。

### 第四階段：GitHub Actions

- 自動測試。
- 每日兩次增量更新。
- 更新完成後產生通知。
- 定期加密備份。
- 更新失敗時發 Discord 告警。

### 第五階段：正式遷移

- 建立 TiDB Cloud。
- 匯入本機書籍、願望清單和追蹤資料。
- 核對出版社數量、預定出版數量與個人資料。
- 部署 Render 並設定 Secrets。
- 手動驗收六家爬蟲和通知後正式切換。

## 建議初始組合

`GitHub 私有 Repository + GitHub Actions + TiDB Cloud Starter + Render Free + Discord Webhook`
