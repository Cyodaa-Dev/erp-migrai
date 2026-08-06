# case04_subcontract — 委外加工發包（T100 → Odoo 19）

金屬沖焊代工廠把表面處理、電鍍、鍛造、CNC、輪牙、橡膠件發包給協力廠。
**在製品離廠之後的狀態**就是靠這幾張表承載的——這個 case 要演示的三個
移轉決策點都長在這裡。

## 這個 case 展示什麼

| | |
| --- | --- |
| 來源表 | `vend_mast`（委外廠商）／`item_mast`（品號，含模具件）／`wc_mast`（工作中心）／`wo_mast`（製令）／`sub_ord`＋`sub_deta`（委外發包單頭／單身） |
| 目標 | `res.partner`／`product.template`／`mrp.workcenter`／`mrp.production`／`purchase.order`／`purchase.order.line` |
| 覆蓋率 | 38 個來源欄位，29 對應、**9 未對應**（76%） |
| 語法 | 與 case03 相同的雙檔維護形狀，另混入 1 條不支援語法（`WHENEVER ERROR CONTINUE`） |

## 🔴 三個刻意的「對不上」

這個 case 的價值不在覆蓋率高，而在**它誠實地說出哪些東西沒地方放**。

### 1. `sub_deta.sub_eta`（預計回廠日）—— 沒有型別的日期

來源是 `VARCHAR(40)`，現場實務填的是「2026/08/15」「約8月中」「月底前」
「等對方回覆」，甚至空白。目標 `purchase.order.line.date_planned` 是 datetime。

規則檔**刻意不宣告**這一欄。理由寫在 `rules/subcontract.yaml` 裡：早期版本
宣告過，結果是 `confidence 100 + transform identity`——因為規則命中會直接
給滿分、跳過型別相容檢查。工具等於拍胸脯說「十成把握直接搬」，然後在 load
階段被「約8月中」炸掉。

拿掉規則後走啟發式路徑，`type_score(VARCHAR → datetime) = 0.0`，誠實落進
`unmapped`。要不要解析、解不出來要擋單還是留空，是導入決策，工具不替客戶選。

> **這一欄就是「委外進度不可見」在資料層的根因。** 進度不是沒人填，是填了
> 之後系統不認得。

### 2. `item_mast.item_hits` / `item_life`（模具累計衝次／設計壽命）

模具在來源系統有品號，衝次與壽命也有欄位。但 Odoo 標準的 `product.template`
兩者都沒有。硬塞進 `weight`／`volume` 之類的數值欄位是錯的——下游會看到一個
語意完全不同的數字。正解是開自訂欄位，而那是導入決策。

> 這兩欄是「模具壽命達成率」KPI 的分子與分母。**沒有欄位，就沒有這個 KPI。**

### 3. `sub_deta.sub_trial`（試模輪次）

同樣沒有對應。它是「調機因人而異」這個現象唯一的量化痕跡——**一家公司最貴的
知識，在現行系統裡沒有欄位可以放。**

其餘未對應：`vend_proc`（協力廠承接的製程別，Odoo 靠 `product.supplierinfo`
而非 partner 欄位表達）、`wc_outs`（是否委外，本語料靠 `OS-`/`MW-` 代碼前綴
表達——是慣例不是欄位，搬遷時必須明講）、`item_kind`（FG/SF/RM/DI 分類要對到
product category，是一張對照表）、`sub_seq`／`sub_wc_no`（Odoo 的採購明細沒有
項次與工作中心的對等語意）。

## 兩個「知道答案」的值轉換

跟上面相反，這兩欄是明確知道怎麼對的，所以宣告了 `value_map`：

- `vend_stus` / `item_stus`：T100 的 `Y`/`N` → Odoo boolean `active`
- `wo_stus`：T100 的 `10`/`20`/`30`/`90` → Odoo `mrp.production.state`
  的 `draft`/`confirmed`/`progress`/`done`

## 資料從哪裡來

`data/seed.sql` 由 `tools/gen_seed.py` 從一份 Odoo 快照**反向產生**，
腳本裡不寫死任何一筆業務資料：

```bash
python3 tools/gen_seed.py <snapshot.json> > data/seed.sql
```

快照取自 Cyodaa 內部 demo 租戶 **ACEX-MW**（金屬沖焊代工，脫敏代號，
非任何真實公司）的 Odoo 種子資料——同樣是合成資料。這樣「語料與 demo 站台
的料號、工作中心一致」是機制保證的，不是靠人記得同步。

品號取自該租戶自己的 BOM 與製令回推（不靠代碼前綴篩選），所以不會混進
其他 demo 租戶的料件。統編、電話、地址皆為合成值。

## 不支援語法（1 處）

- `WHENEVER ERROR CONTINUE`——錯誤處理指示，parser 記一筆 `unsupported_syntax`，
  所屬 FUNCTION 的 `parse_status` 變成 `partial`。

（`DEFER INTERRUPT` 在本語料也有出現，與 case03 相同，同樣不被支援。）

## 目標版本

golden 是對 **`odoo19_catalog.yaml`** 產生的（由 `tools/introspect_catalog.py`
對真實 Odoo 19 執行 `fields_get` 產出）。要對 Odoo 17 跑：

```bash
ERP_MIGRAI_CATALOG=src/erp_migrai/mapping/odoo17_catalog.yaml make update-golden
```

⚠️ 但 Odoo 17 的 catalog 沒有 `mrp.*` 與 `purchase.*` 四個模型，本 case 會失敗。
