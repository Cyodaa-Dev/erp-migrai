# 映射規則檔（`rules/`）

這個目錄放的是**人類知識**：你確定「來源系統裡的這個欄位，就是對到 Odoo
的那個欄位」的規則。命中這裡任何一條規則，可信度（confidence）直接是
100、分層（tier）直接是 `auto`——因為這是人明確教過的，不是猜的。

**貢獻門檻只有一句話：補一條規則就是在這個目錄加幾行 YAML，不需要碰任何
Python。** 載入器（`rules_loader.py`）會讀這個目錄底下**所有** `*.yaml`
與 `*.yml` 檔案（兩種副檔名都會被讀取，混用也沒問題），依檔名排序後合併。
檔名本身不影響行為，只是給人看的分類——你可以照目標模型命名
（`res_partner.yaml`）、照來源系統模組命名，或任何你覺得清楚的方式。

除了 `*.yaml`／`*.yml` 與這份 `README.md` 之外，這個目錄裡的其他檔案
（例如打錯副檔名變成 `.yml.bak`，或不小心把筆記存在這裡）**不會**被當成
規則讀取——但也不會悄悄消失：`migrai map` 的執行摘要會點名這些沒被讀取的
檔案，讓你能發現「咦，這個檔案我以為有生效」。

## 檔案結構

一個規則檔長這樣：

```yaml
version: 1

tables:
  cust_mast:              # 來源資料表名稱
    target_model: res.partner   # 對到的 Odoo 模型

columns:
  cust_mast.cust_no:      # 來源「資料表.欄位」
    target: res.partner.ref     # 對到的 Odoo「模型.欄位」
```

- `version` 目前必須是 `1`，替未來規則檔格式升版保留空間。
- `tables` 是選填的：一個來源資料表整體對應到哪個 Odoo 模型。
- `columns` 底下每一條的 key 是 `來源資料表.來源欄位`，`target` 指向
  `Odoo模型.Odoo欄位`（例如 `res.partner.ref`——因為 Odoo 模型名稱本身
  就含一個點，所以完整的 `target` 字串會看起來像三段，但意義上仍然是
  「模型.欄位」兩段）。

同一個「來源資料表.來源欄位」不能在整個 `rules/` 目錄裡對到兩個不同的
`target`——即使分別寫在兩個不同的檔案裡，載入時也會直接失敗，並且錯誤
訊息會列出兩個衝突規則各自的 `檔名#鍵` 讓你能直接定位。指到**相同**
`target` 則不算衝突（例如你想把同一條規則按分類拆進兩個檔案，或不小心
複製貼上但沒改錯目標），只會取排序後先出現的那一份。

`tables` 底下的規則也適用同一條規則：同一個來源資料表不能在整個 `rules/`
目錄裡被兩個不同的檔案指到不同的 `target_model`，違反時載入時就會失敗、
訊息列出兩個衝突規則各自的 `檔名#鍵`；指到相同的 `target_model` 不算
衝突，只取排序後先出現的那一份。

## 三種 `transform`

大多數規則不需要 `transform`：省略它，等同 `identity`——來源值原封不動
搬到目標欄位。

### `identity`（預設，通常不必寫）

```yaml
columns:
  cust_mast.cust_name:
    target: res.partner.name
```

### `value_map`：離散值對照

來源是幾個固定字碼、目標欄位型別不同（最常見的是「Y/N 字碼 → boolean」）
時用這個。`map` 是必填，`default` 選填，代表沒命中 `map` 任何一個 key
時要用的值。

```yaml
columns:
  cust_mast.cust_stus:
    target: res.partner.active
    transform:
      kind: value_map
      map:
        "Y": true
        "N": false
      default: true
```

### `constant`：固定值

來源沒有對應資料、但目標欄位要填一個固定值時用這個。`value` 是必填。

```yaml
columns:
  cust_mast.cust_kind:
    target: res.partner.is_company
    transform:
      kind: constant
      value: true
```

只有這三種 `kind`；不存在第四種。

**`value`／`map`／`default` 必須是能寫進 `mapping.json` 的純量值**（字串、
數字、布林、`null`，或由它們組成的 list／dict）：YAML 沒加引號的值不一定
跟你以為的一樣。最常見的陷阱是**日期**與**有前導零的數字**：

```yaml
transform:
  kind: constant
  value: 2024-01-01   # 沒加引號 → YAML 解析成日期，不是字串 "2024-01-01"
```

```yaml
transform:
  kind: constant
  value: 0123          # 沒加引號 → YAML 1.1 當成八進位數字，得到 83，不是 123
```

日期那個例子會被載入器直接擋下來並點名是哪個檔案的哪個鍵（因為
`datetime.date` 無法寫進 `mapping.json`）；但**有前導零的數字不會被擋下**
——`83` 本身是完全合法、能寫進 JSON 的整數，載入器沒有理由懷疑它是錯的。
這是 YAML 本身的行為，不是這個工具能替你擋下的錯誤，所以請一律替可能有
前導零的值加上引號，寫成 `value: "0123"`，讓 YAML 把它當純文字讀，不要去
猜它是不是數字。`mapping.csv`（給人審核用的檔案）已經會把 `constant` 的
實際值印在 `reasons` 欄位裡，方便你審核時發現「這個值跟我以為的不一樣」。

## 規則不會覆蓋 catalog

規則檔裡的 `target`（不論是 `tables` 底下的 `target_model`，還是 `columns`
底下的 `target`）**一定要指到 `catalog.py` 載入出來的 Odoo 17 catalog裡
真實存在的模型與欄位**。

如果你的 `target` 打錯字、拼錯欄位名，或指到 catalog 根本沒收錄的模型，
這是**規則的錯**，載入器在載入當下就會丟出 `RuleError`、清楚點名是哪個
檔案的哪個鍵、以及指到了什麼不存在的東西——**絕對不會**被默默忽略、也不
會讓那條規則悄悄失效卻不出聲。載入規則永遠需要先載入 catalog，就是為了
能做這個檢查。

想確認你的規則能不能過，本機跑：

```bash
uv run pytest tests/mapping/test_rules_loader.py -v
```
