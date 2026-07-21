# Google 登入設定（部署階段人工斷點）

程式端已完成，真正部署到測試網址後才需要做以下設定。不要把 Client Secret 貼到 GitHub、Issue 或程式碼中。

## 1. 建立 Google Cloud OAuth 用戶端

1. 到 Google Cloud Console 建立或選取一個專案。
2. 在 Google Auth Platform 填寫應用程式名稱、支援信箱與聯絡信箱。
3. 測試期間可維持 Testing，並把要登入的 Google 帳號加入 Test users。
4. 建立 OAuth Client，Application type 選 **Web application**。
5. Authorized redirect URI 填入：

   `https://你的正式 Render 網址/auth/google/callback`

   網址的 `https`、網域、路徑與尾端斜線必須完全一致。

## 2. 在 Render 設定環境變數

| 變數 | 內容 |
| --- | --- |
| `ANISHELF_PUBLIC_URL` | `https://你的正式 Render 網址`，尾端不要 `/` |
| `ANISHELF_GOOGLE_CLIENT_ID` | Google 建立的 Client ID |
| `ANISHELF_GOOGLE_CLIENT_SECRET` | Google 建立的 Client Secret |
| `ANISHELF_ADMIN_EMAILS` | 管理員 Google 信箱；多個用逗號分隔 |
| `ANISHELF_SESSION_DAYS` | 可省略，預設 30，最高 90 |

## 3. 驗收順序

1. 先只加入站長帳號為 Test user 與 `ANISHELF_ADMIN_EMAILS`。
2. 確認 Google 登入、登出與重新登入。
3. 確認管理員看得到「資料品質」及「更新資料」。
4. 再加入一個非管理員測試帳號，確認看不到管理功能。
5. 兩個帳號分別加入不同書目，確認訂選、系列追蹤與推薦互不相見。
6. 驗收通過後再決定是否把 OAuth App 從 Testing 發布為 Production。

## 安全設計摘要

- AniShelf 不保存 Google 密碼。
- 只要求 `openid email profile` 三個登入用 scope。
- 不保存 Google refresh token 或 access token。
- 網站 session 使用隨機值；資料庫只保存 SHA-256 雜湊。
- 登入流程驗證 `state`、`nonce`、issuer、audience、期限與已驗證信箱。
- 雲端 Cookie 強制 `Secure`、`HttpOnly`、`SameSite=Lax`；所有寫入 API 另驗證 CSRF token。
