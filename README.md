# ERP MIGRAI

**Open-source core tool for migrating Genero 4GL (T100) systems to Odoo 19 Community Edition.**

> "ERP MIGRAI" is a brand name of Cyodaa. The code is licensed under Apache-2.0;
> use of the brand name itself is not covered by that license.

## What exists today

ERP MIGRAI currently ships four pipeline stages, run from the `migrai` CLI:

- `migrai scan` — reverse-engineers Genero 4GL sources (and their SQL schema)
  into a structured `inventory.json`.
- `migrai map` — maps the fields in `inventory.json` onto the Odoo 19 CE
  target catalog, producing `mapping.json` / `mapping.csv`.
- `migrai load` — writes the mapping onto a running Odoo 19 CE instance
  (via Odoo's built-in `load()`), producing `migration_log.jsonl`.
- `migrai verify` — compares what actually landed in Odoo against the
  source data, producing `verification_report.json` / `.md`.

`load` and `verify` are the only stages that talk to an external system,
and only a synthetic SQLite source has been built for them so far — there
is no real T100 sample to validate against (see the corpus note below). A
CI `integration` job exercises the full `scan → map → load → verify`
pipeline end-to-end against a real Odoo 19 CE instance; you can run the
same thing locally with `make demo-full` against your own Odoo.

See the capability table below for what's available today versus planned
versus commercial-only — the "planned" column doesn't exist yet, and the
"commercial" column is not part of this repository.

## Quickstart (5 minutes)

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```console
$ git clone https://github.com/Cyodaa-Dev/erp-migrai.git
$ cd erp-migrai
$ make install
```

Then run the bundled synthetic corpus through both stages:

```console
$ make demo
```

This is the actual, unedited output of that command against the synthetic
`case01_partner` corpus:

<!-- demo-output:start -->
```console
$ make demo
✓ 掃描完成：1 個表、6 個程式單元、0 處不支援語法
  → out/demo/inventory.json
  → out/demo/events.jsonl
✓ 映射完成：7 個來源欄位、對應 7 個（auto 7、suggested 0、needs_human 0）、覆蓋率 100%
  → out/demo/mapping.json
  → out/demo/mapping.csv
  → out/demo/events.jsonl

0 個欄位未對應。

0 個衝突。

  → out/demo/mapping.csv   ← 用試算表打開這份

這份語料是合成的（synthetic: true）。
想試自己的 .4gl：uv run migrai scan --src <你的目錄> --out <輸出目錄>
```
<!-- demo-output:end -->

Open `out/demo/mapping.csv` in a spreadsheet to see the field-by-field
mapping. Want to try your own `.4gl` sources instead?
`uv run migrai scan --src <your-dir> --out <output-dir>`.

## Capability table

| Capability | Available today ✅ | Planned 🗺️ | Commercial 💼 |
|---|---|---|---|
| Reverse engineering (`.4gl` inventory) | ✅ 4 program-unit kinds | More Genero syntax | Customer rule library |
| Data mapping → Odoo 19 | ✅ 9 models, 76 fields | More standard models | Industry templates |
| ETL load + verify (Odoo 19) | ✅ Synthetic SQLite source | — | Oracle source |
| Local web UI | — | 🗺️ | 💼 |
| Migration assessment (module 2) | — | 🗺️ | 💼 |
| Code transformation (module 4) | — | 🗺️ | 💼 |
| Private deployment / implementation consulting | — | — | 💼 |

Three of the rows above are checked "Available today" in this open-source
release: reverse engineering, data mapping, and ETL load + verify.
Everything else in this repository — including the local web UI mentioned
above — doesn't exist yet. "Available today" for the load/verify row means
what's described above: it runs against a synthetic SQLite source and a
CI job exercises it end-to-end against a real Odoo 19 CE; it does not mean
this has been validated against a real T100 environment.

## License

Code is licensed under [Apache License 2.0](LICENSE). Commercial use is
fully permitted (Apache-2.0). The open-source edition and the commercial
edition offer different capabilities — see the table above.

## Corpus / test data

Everything under `corpus/` is synthetic (`synthetic: true`), hand-written
from the public Genero BDL grammar specification. It contains no real
customer source code and no proprietary information of any kind.

## More documentation

- [Roadmap](ROADMAP.md)
- [Contributing](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security policy](SECURITY.md)
- [Commercial edition](COMMERCIAL.md)
- [License](LICENSE)

---

# ERP MIGRAI（繁體中文）

**把鼎新 T100（Genero 4GL）系統遷移到 Odoo 19 Community Edition 的開源核心工具。**

> ERP MIGRAI 為 Cyodaa 之品牌名稱；程式碼採 Apache-2.0，品牌名稱之使用不在授權範圍內。

## 現在有什麼

ERP MIGRAI 目前透過 `migrai` 這支 CLI 提供四個管線階段：

- `migrai scan` ——把 Genero 4GL 原始碼（與其 SQL schema）逆向工程成
  結構化的 `inventory.json`。
- `migrai map` ——把 `inventory.json` 裡的欄位對應到 Odoo 19 CE 目標型錄，
  產出 `mapping.json` / `mapping.csv`。
- `migrai load` ——把對應關係寫進一台執行中的 Odoo 19 CE（走 Odoo 內建的
  `load()`），產出 `migration_log.jsonl`。
- `migrai verify` ——比對 Odoo 裡實際寫入的資料與來源資料，
  產出 `verification_report.json` / `.md`。

`load` 與 `verify` 是唯二會碰外部系統的階段，目前只針對合成的 SQLite
來源建置與測試過——沒有真實 T100 樣本可以拿來驗證（見下方語料聲明）。
CI 有一個 `integration` job 會對著一台真的 Odoo 19 CE 把
`scan → map → load → verify` 整套跑一遍；本機想跑同一套，對著自己的
Odoo 執行 `make demo-full` 即可。

開源版就這四個階段。現在就有什麼、規劃中什麼、商業版才有什麼，見下方能力表——
「規劃中」欄位目前尚未存在，「商業版」欄位不屬於本 repo。

## 五分鐘上手

需要先安裝 [`uv`](https://docs.astral.sh/uv/)，並具備 Python 3.12（含）以上版本。

```console
$ git clone https://github.com/Cyodaa-Dev/erp-migrai.git
$ cd erp-migrai
$ make install
```

然後對內建的合成語料跑一次兩個階段：

```console
$ make demo
```

上方英文段落已完整展示這次執行對合成語料 `case01_partner` 的逐字輸出；
此處不重複貼一份——本專案的測試只驗證第一組 `demo-output` 界標內的內容，
若在這裡再貼一份，第二份就會變成沒人驗證的宣稱。

打開 `out/demo/mapping.csv` 用試算表看逐欄對應。想試自己的 `.4gl`：
`uv run migrai scan --src <你的目錄> --out <輸出目錄>`。

## 能力表

| 能力 | 現在就有 ✅ | 規劃中 🗺️ | 商業版 💼 |
|---|---|---|---|
| 逆向工程（`.4gl` 盤點） | ✅ 4 種程式單元型態 | 更多 Genero 語法 | 客戶規則庫 |
| 資料映射 → Odoo 19 | ✅ 九個模型、76 欄位 | 更多標準模型 | 產業模板 |
| ETL 載入＋驗證 Odoo 19 | ✅ 合成 SQLite 來源 | — | Oracle 來源 |
| 本機 Web UI | — | 🗺️ | 💼 |
| 遷移評估（模組二） | — | 🗺️ | 💼 |
| 程式碼轉換（模組四） | — | 🗺️ | 💼 |
| 私有部署／導入顧問 | — | — | 💼 |

開源版現在有「現在就有」那一欄打勾的三項可用：逆向工程、資料映射、
ETL 載入＋驗證。本 repo 裡其餘全部尚未實作，包含上面提到的本機 Web 介面。
「載入＋驗證」那一列的「現在就有」是指上一節描述的範圍：對著合成 SQLite
來源跑、有 CI job 對真 Odoo 19 CE 端到端跑過一遍——**不代表已在真實
T100 環境驗證過**。

## 授權

程式碼採 [Apache License 2.0](LICENSE) 授權。商業使用完全允許（Apache-2.0）。開源版與商業版功能不同，見上表。

## 語料聲明

`corpus/` 目錄下全部是合成語料（`synthetic: true`），依 Genero BDL 公開
語法規格自行撰寫，不含任何真實客戶原始碼，亦不含任何專有資訊。

## 更多文件

- [路線圖](ROADMAP.md)
- [貢獻指南](CONTRIBUTING.md)
- [行為準則](CODE_OF_CONDUCT.md)
- [安全政策](SECURITY.md)
- [商業版](COMMERCIAL.md)
- [授權條款](LICENSE)
