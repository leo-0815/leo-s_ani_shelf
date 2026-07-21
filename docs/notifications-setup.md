# AniShelf 通知設定

## 通知分工

- Discord：只有管理員內容與系統異常，例如爬蟲部分失敗、Email 寄送失敗。
- Email：每位帳號自行選擇是否啟用，只寄送該帳號的訂選與系列追蹤內容。
- Google OAuth 只提供已驗證的收件地址；AniShelf 不要求 Gmail 信件讀寫權限。

## GitHub Secrets

到 Repository 的 `Settings → Secrets and variables → Actions` 新增：

| 名稱 | 內容 |
| --- | --- |
| `ANISHELF_DISCORD_WEBHOOK_URL` | Discord Incoming Webhook 完整網址 |
| `ANISHELF_SMTP_HOST` | SMTP 主機，例如服務商提供的主機名稱 |
| `ANISHELF_SMTP_PORT` | STARTTLS 通常使用 `587` |
| `ANISHELF_SMTP_USERNAME` | SMTP 帳號或服務商提供的使用者名稱 |
| `ANISHELF_SMTP_PASSWORD` | SMTP 密碼、App Password 或服務商金鑰 |
| `ANISHELF_EMAIL_FROM` | 顯示寄件者，例如 `AniShelf <notify@example.com>` |

不要把任何密鑰放進 `.env.example`、程式碼、Issue、PR 說明或聊天訊息。

## 使用者操作

1. 使用 Google 登入 AniShelf。
2. 開啟「通知設定」。
3. 啟用 Email，選擇上市前 7、3、1 天或上市當天提醒。
4. 選擇是否接收上市日異動與追蹤系列新刊。
5. 儲存後，下一次每日排程開始生效。

同一事件的 Discord 與 Email 各自保留寄送紀錄；工作重跑不會重複寄出。若已有使用者
啟用 Email、但 SMTP 尚未設定，排程會失敗並嘗試透過管理員 Discord 回報原因。

## 驗收順序

1. 先新增 Discord webhook secret，手動執行一次排程確認沒有設定錯誤。
2. 再新增五個 SMTP secrets。
3. 用管理員與一般帳號各自開啟／關閉通知，確認偏好互不影響。
4. 在測試資料建立七天內上市的訂選書，手動執行 workflow。
5. 確認 Email 只寄到已啟用帳號、Discord 只顯示管理員內容。
6. 立即重跑 workflow，確認同一事件不會再次寄送。
