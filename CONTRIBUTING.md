# 貢獻指南

感謝你願意花時間貢獻 ERP MIGRAI。本文件說明開發環境怎麼架起來、三個主要的
貢獻切入點各要怎麼做，以及送 PR 前需要知道的簽署規則。

## 開發環境

```bash
make install   # uv sync：安裝所有開發相依套件
make test      # 單元 + golden 測試（跳過需要 docker 的整合測試）
make lint      # ruff check + ruff format --check
```

送出 PR 前請確認 `make lint` 與 `make test` 都是綠的；CI 會用同一組指令驗證。

## 合成語料聲明

**本專案沒有任何真實客戶的 `.4gl` 原始碼。** `corpus/` 目錄下的一切都是依
[Four Js Genero BDL](https://4js.com/) 的公開語法規格手寫的合成語料，
不含任何真實系統的程式碼或專有資訊。這個聲明對貢獻者也成立：送新的
corpus case 時，請不要貼入或改寫你在客戶專案裡看到的任何原始碼、表名、
欄位名或業務邏輯——即使只是「看起來很像」也不行。所有語料都必須是你
自己依公開語法規格重新寫出來的。

## 三個貢獻切入點

### ① 新增一筆 corpus case

以 [`corpus/case01_partner/`](corpus/case01_partner/) 為範本：

1. 建立 `corpus/caseNN_<主題>/src/`，放進去：
   - 合成的 `.4gl` 原始碼
   - 對應的 `schema.sql`
   - 一個空的 `SYNTHETIC` 標記檔——這不是裝飾用的：`scan` 階段用這個檔案
     是否存在來決定 `inventory.json` 裡的 `synthetic` 旗標是 `true` 還是
     `false`（見 `src/erp_migrai/inventory/extractor.py` 的
     `SYNTHETIC_MARKER`）。缺這個檔，`synthetic` 就會是 `false`，等於
     對外宣稱這是真實客戶資料。
2. 跑 `make update-golden` 產生 `expected/inventory.json` 與
   `expected/mapping.json`。
3. **開啟並逐行閱讀新產生的 golden 檔，人工審過再 commit。** 這是全新的
   未追蹤檔案，`git diff` 對它不會顯示任何內容——空 diff 不代表沒有東西
   要審，只代表這個指令不適用於全新檔案；請直接打開檔案本身讀一遍，
   確認裡面的每一個欄位都是你預期工具該吐出的內容。
4. 跑 `make test` 確認全綠，送 PR。

更完整的目錄結構說明見 [`corpus/README.md`](corpus/README.md)。

### ② 回報 parser 落差

如果你手上有真實的 Genero 4GL 環境，而 `migrai scan` 解析不了某段語法
（回報成 `UnsupportedSyntax`，或直接丟出例外），我們很想知道——但**請不要
上傳你的原始碼**。

請使用專用的
[Parser 落差回報表單](https://github.com/Cyodaa-Dev/erp-migrai/issues/new?template=parser-gap-report.yml)
開 issue，裡面會請你填：

- Genero／T100 版本
- 失敗的語法片段（去識別化：換掉表名、欄位名、任何業務邏輯或客戶特有的
  命名，只留下讓語法失敗的最小結構）
- `scan` 的錯誤訊息與 `E-` 碼
- 勾選確認貼出的片段不含客戶機密

### ③ 新增映射規則

如果你想教工具「來源系統的這個欄位，就是對到 Odoo 的那個欄位」，不需要
碰任何 Python——在 [`src/erp_migrai/mapping/rules/README.md`](src/erp_migrai/mapping/rules/README.md)
描述的目錄裡加幾行 YAML 就完成了。該檔案有完整的格式說明、三種
`transform`（`identity`／`value_map`／`constant`）的寫法，以及規則彼此
衝突時載入器會如何報錯。

## DCO：簽署你的提交

本專案用 [Developer Certificate of Origin](https://developercertificate.org/)
（DCO）而不是簽署貢獻者授權合約（CLA）——DCO 只要求你在提交時聲明「這段
程式碼是我有權貢獻的」，不需要另外跑一次法律簽署流程，對個人貢獻者的
門檻低很多。

每一個提交都必須帶有 `Signed-off-by` 這一行，用 `-s` 旗標讓 git 自動加上：

```bash
git commit -s -m "你的提交訊息"
```

這會在提交訊息尾端加上一行 `Signed-off-by: 你的名字 <你的 email>`。CI 的
DCO 檢查會擋下缺少這一行的提交——如果你的 PR 卡住卻看不出原因，先檢查是不是
漏了 `-s`。
