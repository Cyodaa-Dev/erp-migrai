# 路線圖

本文件說明 ERP MIGRAI 現在有什麼、規劃中的下一步是什麼、以及每一步依賴什麼。
刻意不寫日期或版本號——猜的日期就是另一種「宣稱一件沒被驗證的事」，這正是
`tests/test_docs.py` 這整套機制想擋下來的事。路線圖要交代的是**先後順序背後的
依賴關係**，不是時程表。

## 現在就有

開源核心目前提供四個管線階段，透過 `migrai` 這支 CLI 執行：

- **`migrai scan`** ——把 Genero 4GL 原始碼（與其 SQL schema）逆向工程成
  結構化的 `inventory.json`。
- **`migrai map`** ——把 `inventory.json` 裡的欄位對應到 Odoo 17 CE 目標型錄，
  產出 `mapping.json` / `mapping.csv`。
- **`migrai load`** ——把 `map` 產出的 `mapping.json` 實際寫入一個執行中的
  Odoo 17 CE 環境（走 Odoo 內建的 `load()`），產出 `migration_log.jsonl`。
- **`migrai verify`** ——在載入完成後，比對 Odoo 內的實際資料與來源資料庫
  是否一致，找出載入過程中可能發生的遺漏或轉型錯誤，產出
  `verification_report.json` / `.md`。

`scan`／`map` 可以用 `make demo` 對內建的合成語料實際跑一次，README 的
示範輸出逐字受測試守衛（見 `tests/test_docs.py` 裡的 `test_make_demo_*`
與 `test_readme_demo_output_matches_reality`）。`load`／`verify` 是唯二
會碰外部系統的階段，其正確性無法用靜態 golden 檔比對，只能靠「資料真的
寫進 Odoo 之後查得出來」來驗證，這需要一個跑得起來的 Odoo 17 執行個體——
本機沒有 Odoo 的貢獻者可以用 `--dry-run` 走完 `load` 讀取／轉換／排序／
組 payload 的全部路徑而不連線；完整四段跑一次用 `make demo-full`（需要
一台自己起的 Odoo 17 CE）。`pyproject.toml` 保留了 `integration`（需要
docker Odoo）這個 pytest 標記、`make test` 預設會跳過它，CI 有一個
`integration` job 對著一台真的 Odoo 17 CE 把 `scan → map → load →
verify` 整套跑一遍——本地開發機不會、也不需要常態跑一個 Odoo 容器。

🔴 **「現在就有」不等於「已在真實 T100 環境驗證過」**：`load`／`verify`
目前只針對合成的 SQLite 來源（`corpus/` 底下的語料）建置與測試，尚無
真實 T100 樣本可以拿來驗證這兩段對真實資料的行為（見 README 的語料聲明）。

## 已知限制

以下是已確認存在、但故意排到計劃 3（載入＋驗證）之後才處理的缺陷，
先記在這裡而不是只留在任務報告裡，避免它們隨時間被遺忘。

### `scan` 把 `RECORD LIKE tbl.*` 與下一句合併（parser 缺口）

**重現方式**：

```4gl
FUNCTION f()
    DEFINE p_deta RECORD LIKE ord_deta.*
    DEFER INTERRUPT
END FUNCTION
```

結果：`parse_status` 變成 `"failed"`、**這個函式的 `definitions` 是空的
（定義遺失）**，`inventory.json` 裡多出一筆不支援語法紀錄，snippet 是
合併後的 `'DEFINE p_deta RECORD LIKE ord_deta.* DEFER INTERRUPT'`，
reason 寫「DEFINE 解析失敗：UnexpectedToken」——**這個 reason 怪錯了
對象**，真正的成因不在 DEFINE 語句本身。

**根因**：`src/erp_migrai/inventory/parser.py` 的 `_TRAILING_OPERATOR_
OR_PUNCT` 把 `RECORD LIKE tbl.*` 結尾的那個 `*` 誤判成「乘號還沒打完」，
於是把下一行接續進來當成同一句的延續。**只有後面接著一句 parser
不認得的語句時才會觸發**——後面接合法的 `LET`／`SELECT` 之類語句時，
`DEFINE` 與 SQL 都會被正確地各自記錄，不受影響。

**嚴重度判定：Important，不是 Critical。** 它是「大聲失敗」而不是
「悄悄產生錯的輸出」——`parse_status=failed`、多一筆不支援語法紀錄、
snippet 完整顯示了合併後的內容，使用者看得出這裡有問題，只是查錯方向。
這正是本專案「誠實回報 parser 做不到的事」這條原則要保護的最低限度：
它沒有假裝自己解析成功。

`RECORD LIKE tbl.*` 是標準且在真實 T100 程式裡極常見的 Genero 寫法，
所以這個缺口值得優先處理，但範圍夠獨立，不需要卡在計劃 3 之內。

**處置計畫**：計劃 3（載入＋驗證）合併之後另開一個小 PR 修正
`_TRAILING_OPERATOR_OR_PUNCT` 的判斷條件，並補一個涵蓋這個語法形狀的
`corpus` case，讓它有一個長期存在的回歸測試盯住，而不是只在這裡寫一段
文字說明。

## 規劃中

以下能力尚未實作。本 repo 裡不存在對應的子指令；提到它們時一律用描述性的
說法（例如「本機 Web 介面」），不會寫成 `migrai <子指令>` 的形式，避免看
起來像已經能跑。

### 本機 Web 介面

用瀏覽器操作 scan／map／load／verify 四個階段，取代目前純 CLI 的操作方式。

這一項依賴前面幾個階段的 CLI 介面先穩定下來——Web 介面本質上是這些階段的
一層外殼，底層行為還在變動時先做外殼容易需要重寫。

### 遷移評估（模組二）

在動手 scan／map 之前，先評估一套既有 Genero 系統的遷移範圍與複雜度，
產出給客戶或導入團隊參考的評估報告。

這一項依賴 scan 階段能辨識的語法型態夠廣——評估報告的可信度取決於
scan 對輸入系統的涵蓋率，覆蓋率不夠時做出的評估會失真。

### 程式碼轉換（模組四）

把 Genero 4GL 的程式邏輯（不只是資料結構）轉換成 Odoo 17 對應的實作。

這一項依賴 map 階段先把資料模型層的對應打穩——程式邏輯轉換要引用的
目標欄位／模型命名，來自 map 階段建立的目標型錄，資料層不穩定時邏輯層
沒有穩固的基礎可以對應。

## 商業版

目前的開源／商業分界見 README 能力表與 [COMMERCIAL.md](COMMERCIAL.md)。
至於以上規劃中的階段，哪些未來會進開源核心、哪些只會出現在商業版，
取決於每一項完成時的實際範圍與依賴狀況，這裡不預先劃線。
