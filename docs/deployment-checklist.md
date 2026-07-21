# AniShelf 雲端部署檢查表

## 帳號與一次性人工設定

- [x] 建立 GitHub Repository：`leo-0815/leo-s_ani_shelf`
- [ ] 安裝 GitHub CLI 並完成 `gh auth login`
- [ ] 建立 TiDB Cloud Starter（AWS Singapore）
- [ ] 註冊 Render 並連接 GitHub
- [ ] 建立 Google Cloud 專案與 OAuth consent screen
- [ ] 建立 Discord Incoming Webhook

密碼、資料庫連線密碼、OAuth Client Secret 與 Discord Webhook URL 僅能存入
GitHub Secrets 或 Render Environment，不得提交至 Repository 或貼入工作紀錄。

## 人工審核斷點

1. 雲端相容與 CI：確認架構、免費方案限制及秘密變數清單。
2. 帳號與權限：確認 Google 登入、管理員介面與一般使用者看不到資料品質頁。
3. 通知：確認 Discord 文字、上市前提醒天數與台北時區。
4. 正式切換：核對資料筆數、願望清單與追蹤系列後才切換正式資料庫。

## 自動驗收門檻

- 所有單元測試通過。
- 未登入的寫入請求被拒絕；一般帳號無法讀取資料品質 API。
- 本機 MySQL 與 TiDB 測試資料庫的書目、訂選及系列追蹤數量一致。
- 各出版社在 GitHub Actions runner 上逐家更新；長鴻失敗只列警告，不阻擋其他來源。
- 通知工作重跑不會重複發送同一事件。
- Render `/api/health`、登入、搜尋、訂選與推薦 smoke test 通過。
