# case03_sales_order — 銷售訂單雙檔維護（單頭 + 明細 + 品號主檔）

**合成語料**（`src/SYNTHETIC`）。依 Genero BDL 公開語法規格自建，非任何客戶之真實程式碼。

## 這個 case 展示什麼

| 面向 | 內容 |
|---|---|
| 4GL 語法 | `DATABASE`、`GLOBALS`、`MAIN`、`FUNCTION`、`MENU`／`COMMAND`、`INPUT`、`DECLARE ... CURSOR FOR` + `FOREACH ... END FOREACH` |
| 宣告 | `CHAR(n)`、`INTEGER`、`RECORD LIKE <table>.*`、`LIKE <table>.<column>` |
| 內嵌 SQL | `SELECT ... INTO ... FROM`、`INSERT INTO`、`UPDATE ... SET`、`DELETE FROM`、`DECLARE <cursor> CURSOR FOR` |
| 來源結構 | 三張表：單頭（`ord_mast`）＋明細（`ord_deta`，複合主鍵）＋品號主檔（`prod_mast`）；`cust_mast` 沿用 case01 的定義 |
| 目標 Odoo 模型 | `sale.order`／`sale.order.line`／`product.product`（於 map 階段與載入階段使用） |
| 外鍵 | 兩條，一條在 DDL 明確宣告（`ord_deta.ord_no → ord_mast.ord_no`），一條刻意不宣告（`ord_deta.ord_prod_no → prod_mast.prod_no`，見下） |

## 這個 case 新增了什麼 case01 沒有的東西

- **`CURSOR`／`FOREACH`**：`so_list_deta()` 用 `DECLARE c_deta CURSOR FOR ... FOREACH ... END FOREACH` 逐行讀明細。
- **多層拓撲排序**：`cust_mast → ord_mast → ord_deta` 三層，`ord_mast` 依賴
  `cust_mast`，`ord_deta` 依賴 `ord_mast`（也「應該」依賴 `prod_mast`，見下）。
- **複合主鍵**：`ord_deta` 的主鍵是 `(ord_no, ord_seq)`。
- **兩條產生 `reference` transform 的欄位，但都來自規則宣告**（見下一節）。

## 不支援語法（刻意混入）

`src/so_maintain.4gl` 刻意混入 3 條已知不支援的 Genero 語法，實測（`uv run migrai
scan`）確認每一條都各自產生一筆 `unsupported_syntax`，`reason` 皆為「無法辨識的語句
關鍵字」，且所屬 `FUNCTION` 的 `parse_status` 因此變成 `partial`（不是 `failed`——
同一個 FUNCTION 裡其他語句仍然解析成功，這正是「不支援語法只影響程式盤點、不影響
其他語句」的行為）：

| 行號 | 語法 | 所屬 FUNCTION | 說明 |
|---|---|---|---|
| 47 | `DEFER INTERRUPT` | `so_insert` | Genero 的「延遲中斷」語句，常見於雙檔寫入期間避免使用者按 CTRL-C 打斷單頭／明細的寫入順序 |
| 97 | `TRY` | `so_list_deta` | Genero 的例外處理起始關鍵字 |
| 99 | `CATCH` | `so_list_deta` | Genero 的例外處理分支關鍵字 |

`END TRY`（第 101 行）本身**不會**被記成不支援：首字 `END`落在 parser 的
`KNOWN_IGNORED` 清單裡，parser 不深究 `END` 後面接的是哪一種區塊關鍵字（`FUNCTION`／
`MENU`／`FOREACH`／`TRY`……皆同一套處理）。

這三條是實測過的、真的落在 `genero.lark`／`parser.py` 目前解析範圍之外的構造，不是
憑印象亂挑：驗法與判準見 `.superpowers/sdd/plan3-task-4-brief.md` 的 PRE-FLIGHT 記錄。

## 三張表的關係

```
cust_mast (客戶主檔，單欄主鍵 cust_no)
    ^
    | FK: ord_mast.ord_cust_no → cust_mast.cust_no（DDL 有宣告）
    |
ord_mast (訂單單頭，單欄主鍵 ord_no)
    ^
    | FK: ord_deta.ord_no → ord_mast.ord_no（DDL 有宣告）
    |
ord_deta (訂單明細，複合主鍵 ord_no + ord_seq)
    │
    │ (邏輯上參照，DDL 刻意不宣告 FK)
    v
prod_mast (品號主檔，單欄主鍵 prod_no)
```

`cust_mast` 與 `corpus/case01_partner/src/schema.sql` 的定義完全相同，這裡重複宣告
一份。**理由**：載入階段必須能在單一 case 內跑完拓撲排序與外鍵解析——若 `ord_mast`
參照的 `cust_mast` 只存在於 case01，未來對 case03 單獨做端到端示範（載入＋驗證）時
會因為找不到被參照的表而無法解析。代價是同一張表的 DDL 出現在兩個地方，改一邊要
記得改另一邊；這個取捨已記錄在 `.superpowers/sdd/plan3-task-4-brief.md`。

`src/erp_migrai/mapping/rules/sale_order.yaml` **不重複宣告** `cust_mast →
res.partner`：那條規則已經在 `res_partner.yaml` 裡，兩個規則檔講同一件事只會留下
「改一邊忘另一邊」的地雷。

## `ord_deta.ord_prod_no` 為什麼 DDL 沒有 FK

`schema.sql` 的 `ord_deta` 只宣告了 `FOREIGN KEY (ord_no) REFERENCES ord_mast
(ord_no)`，**刻意不宣告** `ord_prod_no → prod_mast.prod_no`。這是真實鼎新 T100 更
常見的形狀：代碼式表名、參照關係只存在於程式邏輯與人的知識裡，DDL 本身没有 FK
約束。`ord_deta.ord_no` 的 FK 則正常宣告，用來演練「DDL 有宣告 FK、規則也宣告
`reference`、兩者一致，不觸發 `E-2001`」這條路。

## `reference` transform：兩條，都來自規則宣告

🔴 本計劃原本設想「至少一條 `reference` 由 DDL 外鍵自動產生」，但 Task 3 實測證明
「從 DDL 外鍵自動產生 `reference`」是永遠打不開的死碼（`typing_rules._with_relational`
對所有來源型別家族無條件把 `many2one` 的 `type_score` 固定為 `0.0`，
`confidence = 100 × name_score × type_score` 恆為 `0`，引擎在選出目標欄位之前就
早退），已被整段拿掉。**現在 `reference` 只有一個來源：`sale_order.yaml` 規則檔
明確宣告**——這正是設計 spec P3-5 本來就寫明的主路徑（貢獻點③）：

| 來源欄位 | 目標欄位 | `ref_table` | `ref_column` | DDL 是否也宣告了同一條 FK |
|---|---|---|---|---|
| `ord_mast.ord_cust_no` | `sale.order.partner_id` | `cust_mast` | `cust_no` | 是（一致，不觸發衝突） |
| `ord_deta.ord_no` | `sale.order.line.order_id` | `ord_mast` | `ord_no` | 是（一致，不觸發衝突） |
| `ord_deta.ord_prod_no` | `sale.order.line.product_id` | `prod_mast` | `prod_no` | 否（DDL 刻意沒宣告，只能靠規則補——真實 T100 最常見的形狀） |

## `ord_mast.ord_no` 對到 `name`、`ord_cust_ref` 對到 `client_order_ref`

這個語料原本把 `ord_mast.ord_no`（T100 自己的訂單編號）對到
`sale.order.client_order_ref`（客戶端參照欄位），而 `ord_mast.ord_cust_ref`
（客戶自己的訂單編號／PO 號）沒有宣告規則、落回名稱／型別啟發式，也算出同一個
`client_order_ref`（`confidence=30`／`tier=needs_human`）。這是配錯了：catalog
對 `client_order_ref` 的定義是「客戶自己的訂單編號（客戶端參照），用於單據
對帳」——`ord_cust_ref` 才是這個定義說的東西，`ord_no` 是我們自己的單號，不該
佔用它。

**Task 4.5（昴君裁決）在 map 階段新增的目標欄位唯一性檢查，第一時間在這份語料裡
抓到了這個真缺陷**：`ord_no`（規則，confidence 100）與 `ord_cust_ref`（啟發式，
confidence 30）同時對到 `client_order_ref`，記進 `mapping.json` 的
`conflicts`（一筆 `kind == "target"`，`column` 為 `null`，`candidates` 是排序後
的 `["ord_cust_ref", "ord_no"]`），並發 `E-2001`，讓 `migrai map` 對這個 case
exit 1。這正是這項檢查存在的理由：抓出「同一筆記錄的同一個欄位到了載入階段會被
後寫的覆蓋、且沒有機制決定哪一個贏」這種悄悄發生的資料遺失。

**修正**：`src/erp_migrai/mapping/rules/sale_order.yaml` 已經改為
`ord_cust_ref → client_order_ref`（新增明確規則，confidence 100／auto）、
`ord_no → name`。衝突的根源消失，`mapping.json` 的 `conflicts` 因此變回
`[]`——這不是把檢查關掉，檢查機制仍然活著（見
`tests/mapping/test_engine.py` 裡 Task 4.5 新增的測試），只是這個具體的示範
案例已經不再示範衝突，而是示範衝突被抓到、被修正的完整流程。

🔴 `sale.order.name` 是 `required=True` 且有
`default=lambda self: _('New')`（Odoo 內建的自動編號機制，Odoo 17 原始碼
`addons/sale/models/sale_order.py` 第 51～55 行）。用匯入資料寫入 `name` 時
Odoo 會不會忽略它、改用自動編號覆寫掉——這件事本任務沒有 Odoo 環境可以實測，
留給未來載入階段的 CI 整合測試驗證。若實測發現 Odoo 真的會覆寫：`ord_no` 的
可追溯性依然不會遺失，因為 `ord_no` 是 `ord_mast` 的主鍵，載入階段會用它組成
external_id（`migrai.<source_id>.ord_mast.<ord_no>`），來源系統的原始單號永遠
可以透過那個 external_id 追溯回去。

## 其餘未對應欄位（`unmapped`）

還有兩欄没有任何規則、啟發式也算不出任何相容對應，誠實地落在 `mapping.json` 的
`unmapped`：

| 來源欄位 | 原因 |
|---|---|
| `prod_mast.prod_spec`（品號規格說明） | `product.product` 這份精簡 catalog 沒有收錄適合承接自由格式規格文字的欄位 |
| `ord_deta.ord_seq`（明細行序號） | `sale.order.line` 沒有序號欄位；唯一有字面交集的候選 `order_id` 是 `many2one`，型別分數恆為 `0` |

最終 `mapping.json` 的 `stats`：24 個來源欄位、22 筆對應（22 筆皆 `auto`）、
2 筆 `unmapped`、0 筆 `conflicts`，覆蓋率 92%（高於 `E-2002` 的 80% 門檻，不會
觸發覆蓋率過低事件）。

## 金額手算（`ord_qty × ord_price`）

`data/seed.sql` 的明細金額刻意取整數，方便肉眼核對，不必只相信程式：

| 訂單 | 明細 | 數量 × 單價 | 小計 |
|---|---|---|---|
| `O0001`（客戶 `C001`） | `P001` | 10 × 1000.0000 | 10000.0000 |
| `O0001`（客戶 `C001`） | `P002` | 4 × 250.0000 | 1000.0000 |
| **`O0001` 合計** | | | **11000.0000** |
| `O0002`（客戶 `C002`） | `P001` | 5 × 1000.0000 | 5000.0000 |
| `O0002`（客戶 `C002`） | `P003` | 2 × 5000.0000 | 10000.0000 |
| **`O0002` 合計** | | | **15000.0000** |

兩張訂單各有 2 行明細（皆 ≥ 2 行，證明 `one2many` 分組不是只有一行的巧合）。
`P003`（備用馬達）`prod_stus = 'N'`，用來驗 `value_map` 走 false 分支
（`product.product.active = false`）。

## 檔案

| 檔案 | 用途 |
|---|---|
| `src/so_maintain.4gl` | 合成 Genero 程式（單頭＋明細雙檔維護＋CURSOR） |
| `src/schema.sql` | T100 風格來源表 DDL（`cust_mast`／`prod_mast`／`ord_mast`／`ord_deta`） |
| `data/seed.sql` | 合成來源資料。`data.sqlite` 不進 git，於載入階段由此生成 |
| `expected/inventory.json` | golden：`migrai scan` 應該吐出的內容 |
| `expected/mapping.json` | golden：`migrai map` 應該吐出的內容 |
| `../../src/erp_migrai/mapping/rules/sale_order.yaml` | 這個 case 用到的映射規則（`prod_mast`／`ord_mast`／`ord_deta` 三張表） |
