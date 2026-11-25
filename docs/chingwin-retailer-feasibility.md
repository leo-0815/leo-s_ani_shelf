# 青文通路備援可行性評估

以青文 [10 月紙本](https://www.ching-win.com.tw/about-news-detail/208)、[10 月電子](https://www.ching-win.com.tw/about-news-detail/209)、[11 月紙本](https://www.ching-win.com.tw/about-news-detail/210)和[11 月電子](https://www.ching-win.com.tw/about-news-detail/211)公告的同一組 10 筆樣本為基準。只核對可見的書目欄位；未執行通路批量請求，未量測完整覆蓋、上架延遲、GitHub runner 可用性或再利用授權。

| 來源 | 本次人工核對 | 建議 |
| --- | --- | --- |
| 金石堂 | 同版 ISBN 確認 5 筆（4 一般、1 特典） | 優先詢問是否可提供青文書目匯出；保留通路日期來源 |
| 三民 | 3 筆有標題、卷、媒體和版本的清單線索，未核到這 3 筆的單品 ISBN | 紙本備援候選；清單其餘結果不視為缺貨或不存在 |
| 琅琅讀墨 | 1 筆數位單話可核題名和話數；公告沒列 EISBN，不計為同 ISBN 命中 | 數位專項候選，不拿來補紙本 |
| 國圖 | 上一輪以 7–10 月資料確認 5 筆一般版，另 2 筆同 ISBN 但未確認版本 | 開放資料只能補線索，不當日更或正式上市日 |

這些是查到的**證據筆數**，不是通路的成功率。三民獨立的第 9 卷控制樣本有單本 ISBN，但不在 10 筆基準裡，不能算進命中。

## 值得保留的差異

- 金石堂把《Re:從零開始的異世界生活 短篇集》(11)的[紙本](https://www.kingstone.com.tw/basic/2018612751263/?actid=&lid=book_new_index_push)和[電子書](https://www.kingstone.com.tw/basic/2800000353575/?actid=&lid=book_publish_press_new_08025)分成兩個商品，兩頁均列同一 ISBN。媒體格式必須核對，不能只以 ISBN 合併。
- 《Ω的花燭 共鳴戀情》(01)特典版同 ISBN，卻是[金石堂 2026-10-21](https://www.kingstone.com.tw/basic/2019462281276/?actid=book_normal_dh&lid=book_normal_pa)和[青文 2026-11-10](https://www.ching-win.com.tw/product-detail/10496401A)，差 20 天。這是兩個頁面顯示值的差距，不是上架延遲。零售出版日、出貨日、官網日期分開保存；BL 和 R18 也不能互推。
- 三民[青文牌表](https://www.sanmin.com.tw/search?pu=%E9%9D%92%E6%87%87)可作發現入口，但牌表可能連其他近似出版社一起返回；逐筆核對。三民的[單品控制樣本](https://www.sanmin.com.tw/product/index/015601375)有 ISBN、出版日、裝訂、版次、作者，不能把單品欄位完整性推廣到整個清單。
- 琅琅讀墨的[《破曉之歌》第 37 話](https://reading.udn.com/ebook/store/store_product.do?pid=183967&source=%E6%BC%AB%E7%95%AB%E6%9C%80%E6%96%B0)對上青文電子公告，來源出版社標為悅文社。該數位證據只對應此話，不外推到紙本、其他話數或青文所有電子書。

## 自動取用決策

目前沒有確認到金石堂、三民或琅琅讀墨對這個用途開放的書目 API／匯出，也沒有取得授權。金石堂[分紅大聯盟](https://www.kingstone.com.tw/partner/ap/)是導購計畫，不是書目資料授權；可透過[官方聯絡頁](https://www.kingstone.com.tw/qa/callme/)詢問。三民另有[服務條款](https://www.sanmin.com.tw/static/termsofservice)與[異業合作入口](https://www.sanmin.com.tw/other/cooperation)。未替使用者聯繫或提交表單。站方若提供允許取用的檔案或端點，再確認可用欄位、使用與展示範圍、更新頻率、請求上限，以及青文紙本／特裝／電子的涵蓋程度。

本 PR 的比較工具只讀人工樣本，不會連通路。原本未上線的金石堂即時請求程式已移除，既有 #63 的正式流程不改。重跑：

    python -m app.chingwin_coverage --baseline tests/fixtures/chingwin_oct_nov_2026_sample.json --retailer-samples tests/fixtures/chingwin_retailer_manual_samples.json --skip-ncl --out chingwin-retailer-sample-report.json

下一階段：獲准後，先在 GitHub runner 用有限請求記錄每日同版樣本首次出現時間（不寫庫）；沒有授權或穩定性、欄位正確性不足，維持目前日更與書目不變。若只獲得國圖開放資料，做月度候選，不宣稱恢復日更。

## 國圖月度候選（唯讀）

國家圖書館的[臺灣出版新書預告書訊](https://data.gov.tw/dataset/6730)依[政府資料開放授權條款－第 1 版](https://data.gov.tw/license)釋出。資料月份是 ISBN 申請資料檔的月份，不是上市月份。報告保留資料提供者、資料集、授權及原始 CSV 連結。

合併到預設分支後，GitHub Actions 每月 6 日台灣時間 09:17 排程執行，也可手動觸發。流程只讀取資料集目錄及最近三個發布月份的 CSV，報告會標示最近列出的月份和實際讀到的月份。報告不查 AniShelf 書目，候選不等於缺書；ISBN 和紙本／電子分開核對。格式不明、同 ISBN 同媒體但卷次或版本衝突的資料不進候選。預訂月份、版次線索都不會覆寫官網商品資料；不採用封面或分級，不使用資料庫憑證，也不更動每日更新。

    python -m app.chingwin_coverage --ncl-months 3 --monthly-candidates --baseline tests/fixtures/chingwin_oct_nov_2026_sample.json --out chingwin-ncl-review.json
