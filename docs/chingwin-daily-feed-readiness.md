# 青文｜每日備援接入驗收

目前沒有確認可自動每日取用的青文通路書目來源；國圖月報也不能補正式上市日。這個工具只讀本機測試資料並印出驗收結果，不連任何通路、不寫書目，也不會啟用備援。

1. 先取得出版社或通路對每日自動取用、允許欄位、涵蓋媒體和請求額度的確認。日後若需散布或對外展示，再另外核對允許範圍。證明存放在私人環境；驗收資料只填不含帳密或機密的參考編號，不提交往來信件。
2. 在獲准範圍內留下至少七個連續日期的每日檢查結果。遇攔阻停用該來源，不換身分或重試。用同一觀察期間的出版社資料作基準，記錄雙方「首次觀察到」的帶時區時間，不用上市日、出貨日或印製日反推。
3. 每個允許的類別至少驗五本不同書；至少 80% 能在官網參考觀察後 48 小時內對到同版，才可列為人工審查候選。這是小規模試行門檻，不是整體涵蓋率或自動核准。

執行：`python -m app.chingwin_feed_readiness path/to/local-observations.json`。輸入 JSON 有三個欄位：

- `permission`：`provider_confirmed`、`allows_automated_daily_read`、`evidence_reference`、`formats`（`print_standard`、`print_special`、`ebook` 中有獲准的類別）。
- `daily_checks`：每日一筆 `date`（台灣日期）、`status`（`ok`、`unavailable`、`blocked`）。
- `samples`：每筆含 `official` 和 `feed`（查不到填 `null`）。書目欄位為 `publisher`、`title`、`volume`、`media`（`print`／`ebook`）、`edition`（如 `standard`、`bonus`、`digital`）、`isbn` 或 `gtin`、`first_seen_at`（須附時區）。不需要商品網址、封面或分級。

工具會把 ISBN 和 GTIN 分開核對，也不會把同 ISBN 的紙本與電子書算成同版。即使通過，仍要人工核對授權、對照版次與資料用途，再另行決定是否啟動唯讀觀察；正式寫入及回退策略不在本次範圍。
