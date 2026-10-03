## PR27：出版社多選與一般向偏好

Library、已訂選、我的藏書與系列書架上方可同時勾選任意多家出版社，按「套用篩選」生效；全選／重設顯示全部，清除勾選後套用顯示零筆。更換篩選回到第一頁，翻頁及切換書架保留選擇。舊 API 的 `publisher=spp` 仍可使用；多選使用 `publishers=spp,tongli`，明確空集合使用 `publishers=none`。

「偏好設定」新增一般向開關，預設不勾選以保留既有顯示習慣。勾選並儲存後暫時排除青文官方書目；後端在分頁、搜尋、詳情、推薦、系列、近期上市和資料品質查詢時處理，不僅隱藏前端卡片。雲端設定依帳號存於 `user_preferences`，本機設定獨立保存。這不是書籍年齡分級，無法保證其他出版社皆無成人內容；私人手動書籍無官方出版社代碼，暫不做推定分類。既有藏書、訂選、追蹤不刪除；備份、爬蟲同步及 Email／Discord 通知不受此瀏覽偏好影響。

無新執行期套件；新資料表由啟動／`python anishelf.py migrate` 建立。UI 回歸測試：`node scripts/ui_filters_smoke.cjs`（開發環境 Playwright＋Edge）；Python 回歸：`python -m unittest discover -s tests`。

## PR26：追蹤整系列

商品詳情可選「只追蹤新書」或「所有書籍（含既有卷數）」，系列詳情也可切換範圍。同出版社／同系列仍依漫畫與小說分開。全系列自動納入尚未擁有的書籍；既有預購狀態、備註與優先度不覆蓋。停止追蹤不刪既有訂選與藏書。歷史補登舊書不作新刊通知；新刊事件僅限追蹤後收錄，且預定出版或上市日在最近 30 天內的書。

## PR25：新增藏書

「我的藏書」新增入口支援搜尋書目／手動新增。書名是自建藏書唯一必填欄位，無封面時顯示書名前六字。自建書籍可編輯、刪除與匯出，僅限自己的帳號存取，不參與公共書目同步或新刊通知。JSON 還原自建藏書會建立新紀錄；重複匯入可能產生重複藏書。

## PR24：獨立藏書

已購入書籍會遷移至獨立藏書表。愛心只代表關注，取消愛心不會移除藏書；刪除藏書需要另外確認。各帳號的藏書、實付價格與私人備註各自隔離。

# AniShelf

## Local-to-cloud catalog sync

The machine-to-machine API transfers only publisher and book metadata. Personal account data,
wishlists, followed series, sessions, and notification settings remain cloud-only. The API stays
disabled until a 32-character-or-longer `ANISHELF_CATALOG_SYNC_TOKEN` is configured on Render.

See [catalog sync API setup](docs/catalog-sync-api.md) for endpoint, token, snapshot, incremental
cursor, and read-only difference-check instructions.

台灣漫畫與輕小說上市資訊追蹤工具。首版採單機 Web 介面、既有 MySQL 與原生
HTML/CSS/JavaScript，避免 Node、Redis、Docker、瀏覽器引擎及圖片下載造成容量膨脹。

## 目前狀態

- 線上版：<https://anishelf-wmcu.onrender.com>；本機版仍可獨立使用，不會被雲端部署取代
- Google OAuth 帳號登入；一般帳號擁有個人書架與通知，管理員另可查看資料品質與更新工具
- Library、搜尋與出版社／類型／狀態篩選
- 完整分頁、日期區間、版本、訂選狀態與多種排序
- 系列書架：以安全正規化與可擴充別名字典合併一般版／限定版；同系列的漫畫與小說仍分開顯示，並提示可能缺少的集數
- 已訂選清單：想買、已預購、已購入、暫不購買、優先度、店家、訂單與實付價格
- 我的藏書：每個帳號獨立整理已購入書目、購入日期、紙本／電子格式、收藏系列與累計實付
- 系列追蹤：新收錄的同系列書籍會自動加入想買，現有預定書也會一起加入
- 為你推薦：依已購入書目與已追蹤系列推薦作品，可忽略項目，購入後也會詢問是否追蹤系列
- 書籍詳細頁：底部顯示「你可能會喜歡」，可直接查看或加入想買
- 近期上市時間軸、瀏覽器通知與 `.ics` 日曆匯出
- 每位帳號獨立的 Email 上市提醒、日期異動與追蹤系列新刊通知
- 管理員專用 Discord 排程摘要與爬蟲／Email 寄送異常提醒
- 資料品質頁：缺作者、ISBN、日期、類型與可疑舊日期
- 完整 JSON 備份、CSV 書庫／訂選匯出
- 書籍詳細資料與出版社原始連結
- 書籍日期與狀態異動歷史
- 手動背景增量更新、進度與來源健康狀態
- 啟動時自動增量更新；六小時內已執行過便略過，仍可手動強制更新
- 已完成一次官方公開歷史分頁的大型回填；每家保留日期與頁面游標
- 搜尋支援部分書名、作者、ISBN，以及用空格分隔的多個關鍵字
- 青文出版社公開封存頁的月度預定表（月份精度）
- 東立出版社預定出書表（官方預定、日期尚未公布）與商品明細
- 台灣東販漫畫／輕小說公開目錄與商品詳細頁（日期精度）
- 台灣角川商城公開月份分類與商品詳細頁（日期精度）
- 東立漫畫／小說公開新書分頁與商品詳細頁（日期精度）
- 尖端官方上市表（日期精度，包含表內再版項目）
- 長鴻公開封存分頁月度新書表（日期精度，跨月重複自動去除）

系統不保存原始 HTML，也不下載封面；只保存必要書目、來源網址、內容雜湊與日期異動。
線上每日排程目前更新台灣東販、青文、台灣角川、東立與尖端；長鴻程式及既有資料
仍保留，但因官方頁面失效且使用優先度較低，暫不納入線上排程。

## 第一次使用

需求：Windows、Python 3.10 以上、MySQL 8.0 以上。

```powershell
python anishelf.py install
python anishelf.py setup
python anishelf.py run
```

`setup` 會要求輸入一次 MySQL 管理帳號密碼，用來建立 `anishelf` 資料庫和權限受限的
`anishelf_app` 帳號。管理密碼不會保存；應用程式密碼只寫入本機 `.env`，該檔案已被
Git 忽略。

啟動後開啟 <http://127.0.0.1:8765>。

Windows 也可以直接雙擊專案資料夾內的 `啟動 AniShelf.bat`。啟動器會等網站確實
就緒後才開啟瀏覽器；啟動後請保持命令視窗開啟，關閉該視窗就會停止本機網站。
若 AniShelf 已經在執行，重複雙擊只會開啟網站，不會再建立第二個伺服器。

網址必須使用 **HTTP**：<http://127.0.0.1:8765/>，不是 `https://`。若啟動失敗，
命令視窗會保留錯誤提示，詳細內容則會寫入專案根目錄的 `anishelf.err.log`。

## 更新方式

日常使用不需要額外操作。啟動程式或按介面的「更新資料」都會執行增量更新：

- 月份文章來源只處理最新游標之後的新文章。
- 商品來源只掃最新清單，商品編號已存在就不再下載詳細頁。
- 同一月份後補的新商品仍能靠商品編號辨識並寫入。

命令列也可手動執行與檢查：

```powershell
python anishelf.py update
python anishelf.py status
python anishelf.py migrate
python anishelf.py reindex
python anishelf.py restore .\anishelf-export.json
```

`python anishelf.py backfill` 是一次性的全來源歷史回填；也可指定單一來源，例如
`python anishelf.py backfill tongli`。平常請使用 `update`，避免重掃歷史分頁。

`reindex` 只重新計算系列名稱、系列識別鍵、卷數和版本，不連線出版社，也不會刪除書目。系列比對不使用模糊結果直接合併；規則與資料庫中已核准的別名才會生效。

`restore` 可將網站匯出的 JSON 還原至 MySQL；既有書目會合併，訂選狀態、優先度、
店家、訂單、價格與系列追蹤也會一併恢復。

`migrate` 會套用目前版本需要的資料表與欄位，可在部署前安全地重複執行。

## 雲端部署準備

專案根目錄的 `render.yaml` 定義 Render Singapore Web Service。Render 會自動提供
`PORT`；雲端模式綁定 `0.0.0.0`、使用 TiDB TLS 主機名稱驗證，並關閉網站啟動時的
背景更新，避免 Web Service 重啟時產生重複爬蟲。定時更新之後由 GitHub Actions 負責。

Web Service 使用惰性 TiDB 連線池，預設最多 4 條連線；啟動時不預先連線，也沒有任何
防休眠或定時暖機請求。Render 閒置休眠行為保持不變。Render 的高頻 health check 會在
應用程式內短暫快取，登入 session 的 `last_seen_at` 也只會定期更新，以減少遠端 TLS
連線及不必要的資料庫寫入。可用 `ANISHELF_DB_POOL_SIZE`、
`ANISHELF_HEALTH_CACHE_SECONDS`、`ANISHELF_SESSION_TOUCH_MINUTES` 調整，Blueprint
預設值分別為 4、20 秒、10 分鐘。

Render Blueprint 建立時需由管理者直接填入下列秘密值，請勿寫進 Git 或聊天室：

- `ANISHELF_DB_HOST`
- `ANISHELF_DB_NAME`
- `ANISHELF_DB_USER`
- `ANISHELF_DB_PASSWORD`

每日更新與通知由 GitHub Actions 在台灣時間 08:15 執行。Discord webhook 與 SMTP
寄件設定只放在 Repository Actions Secrets；完整欄位與驗收流程請參考
`docs/notifications-setup.md`。手動執行 `Scheduled catalog update` 時可勾選
`test_email_only`，只寄一封 Gmail SMTP 測試信，不更新出版社資料，也不建立正式通知
紀錄。

每位使用者可在網站的「通知設定」自行啟用 Email，收件地址使用已驗證的 Google
登入信箱；通知包含上市前 7、3、1 天與上市當天、日期異動及追蹤系列新刊。所有帳號
可從設定頁寄送 Email 測試信；管理員另可查看 Discord 連線狀態並寄送測試訊息。Discord
仍為管理員專用的排程摘要與錯誤告警。

Render Free 封鎖 SMTP 連接埠，因此正式網站的 Email 測試使用 HTTPS 觸發既有 GitHub
Actions 工作，再由 Actions 透過 Gmail SMTP 寄到目前帳號。Render 僅保存一枚限制於
本 repository 且只允許 Actions 寫入的 fine-grained token，不保存 Gmail App Password。

`.github/workflows/ci.yml` 會在 push 與 Pull Request 上使用 Python 3.10、3.12 執行
完整測試。部署建議保持 `checksPass`，只有 CI 通過才更新正式服務。

## 測試

```powershell
python -m unittest discover -v
```

## 架構

- `app/schema.sql`：MySQL 資料表與索引
- `app/repository.py`：Library、訂選清單與日期歷史
- `docs/catalog-sync-foundation.md`：本機／雲端書目同步游標與內容分級資料契約
- `app/sources/`：每家出版社的獨立轉接器
- `app/crawler.py`：低頻率背景更新工作
- `app/server.py`：小型本機 HTTP／JSON API
- `web/`：無建置步驟的原生介面

## 容量策略

- 唯一第三方套件為約 45 KB wheel 的 PyMySQL。
- 每次來源回應限制 6 MB，只在記憶體解析後立即丟棄。
- 不保存原始頁面、不快取封面、不加入無頭瀏覽器。
- 封面直接使用來源網址並延遲載入，不佔用本機圖片容量。
- 大型回填只需執行一次；日常更新以游標與來源商品鍵略過歷史頁及既有詳細頁。
- 書目異動以輕量變更序號記錄；不保存原始頁面，也不把帳號資料納入本機同步。
- Library 查詢上限每次 200 筆；資料表保留必要索引。
# 近期更新

尖端每日出書資料改讀官網連結的公開出書表，自動發現近期「動漫」分頁。書码維持原識別，跨月重讀以捕捉延期；來源失敗時保留警告並嘗試商城商品備援。

