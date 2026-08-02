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

## `ord_mast.ord_no` 為什麼對到 `client_order_ref` 而不是 `name`

`sale.order.name` 是 `required=True` 且有 `default=lambda: _('New')`（Odoo 內建的
自動編號機制）。`ord_mast.ord_no` 要不要覆寫 `name`，牽涉到「用匯入資料寫入 `name`
時 Odoo 會不會忽略它、改用自動編號覆寫掉」——這件事本任務沒有 Odoo 環境可以實測，
留給未來載入階段的 CI 環境驗證。

本任務先把 `ord_no`（T100 自己的訂單編號）對到 `sale.order.client_order_ref`
（客戶端參照欄位）：這樣至少能把來源系統的原始單號寫進一個明確存在、且不會跟
Odoo 自動編號機制衝突的欄位，維持可追溯性；等載入階段實測確認 `name` 的行為後，
再決定要不要改接到 `name`。

### 已知的副作用：`ord_cust_ref` 落到 `needs_human`

`ord_mast` 另外還有一欄 `ord_cust_ref`（客戶自己的訂單編號／PO 號），語意上其實
更適合叫做「client order ref」；但這個名字已經被上面的 `ord_no` 決定占用了，
`sale.order` 這份 catalog 裡沒有第二個適合承接它的欄位。`sale_order.yaml`
**刻意不對它宣告規則**，讓它落回名稱／型別啟發式：

```
ord_mast.ord_cust_ref -> sale.order.client_order_ref  confidence=30  tier=needs_human
```

這是引擎的真實行為，不是本任務的失誤：`ord_cust_ref` 正規化後與
`client_order_ref` 有部分 token 交集（`order`／`reference`），啟發式因此算出一個
分數（`0.6 × 交集/聯集`），但因為分數只有 30（< `SUGGESTED_THRESHOLD=70`），被分到
`needs_human`，不會被當成可信的自動對應——這正是分層機制存在的理由：字面上看起來
沾親帶故、但不夠確定的猜測，要留給人審核，不能悄悄當真。目前的引擎沒有「同一個
目標欄位只能被一個來源欄位對應」的檢查，`ord_no`（confidence 100／auto）與
`ord_cust_ref`（confidence 30／needs_human）因此會同時出現在 `mapping.json` 的
`mappings` 裡、都指向 `client_order_ref`——這是已知的 MVP 限制，留給未來任務視情況
決定是否要補上目標欄位唯一性檢查。

## 其餘未對應欄位（`unmapped`）

除了上述副作用，還有兩欄没有任何規則、啟發式也算不出任何相容對應，誠實地落在
`mapping.json` 的 `unmapped`：

| 來源欄位 | 原因 |
|---|---|
| `prod_mast.prod_spec`（品號規格說明） | `product.product` 這份精簡 catalog 沒有收錄適合承接自由格式規格文字的欄位 |
| `ord_deta.ord_seq`（明細行序號） | `sale.order.line` 沒有序號欄位；唯一有字面交集的候選 `order_id` 是 `many2one`，型別分數恆為 `0` |

最終 `mapping.json` 的 `stats`：24 個來源欄位、22 筆對應（21 筆 `auto` + 1 筆
`needs_human`）、2 筆 `unmapped`、0 筆 `conflicts`，覆蓋率 92%（高於 `E-2002` 的
80% 門檻，不會觸發覆蓋率過低事件）。

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
