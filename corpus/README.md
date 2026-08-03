# corpus — 合成語料庫

**貢獻點①：加一個 case 資料夾，就自動多一組測試，不需要改任何程式碼。**

## 為什麼是合成的

本專案沒有真實的 T100／`.4gl` 樣本。所有語料都依
[Four Js Genero BDL](https://4js.com/) 的**公開語法規格**自行撰寫，
標記於各 case 的 `src/SYNTHETIC`，並在 `inventory.json` 中以 `"synthetic": true` 呈現。
**不含任何客戶之真實程式碼或鼎新專有資訊。**

⚠️ 因此本工具的 parser **尚未在真實 T100 程式碼上驗證**。
若你手上有真實環境，請使用專用的
[Parser 落差回報表單](https://github.com/Cyodaa-Dev/erp-migrai/issues/new?template=parser-gap-report.yml)
開 issue，**只回報落差**（失敗的語法片段 + 錯誤訊息），不需要上傳原始碼。

## 一個 case 的結構

```
corpus/caseNN_<主題>/
├─ README.md                這個 case 展示什麼語法、什麼坑、不含什麼
├─ src/SYNTHETIC            合成語料聲明（缺此檔則 synthetic 會是 false）
├─ src/*.4gl                合成 Genero 程式
├─ src/schema.sql           來源表 DDL
├─ data/seed.sql            合成來源資料（data.sqlite 不進 git，由 make corpus 生成）
├─ expected/inventory.json  golden：migrai scan 應該吐出的內容
└─ expected/mapping.json    golden：migrai map 應該吐出的內容
```

`src/` 就是 `scan` 階段會讀的東西：`SYNTHETIC`、`*.4gl`、`schema.sql`，僅此而已；
`scan` 在 `src/` 裡找到卻不認得的檔案一律會被回報成未消化的 `UnsupportedSyntax`，
所以任何不屬於 scan 契約的檔案（例如給日後載入階段用的 `seed.sql`）都必須放到
`src/` 之外（例如 `data/`），否則會被誤判為語法缺口。

## 新增一個 case

1. 依上表建立資料夾與檔案。
2. 跑 `make update-golden` 生成 `expected/inventory.json` 與 `expected/mapping.json`。
3. **開啟並逐行閱讀生成的 `expected/inventory.json` 與 `expected/mapping.json`**
   ——它們就是你對工具行為的斷言。這是第一次產生的全新檔案（尚未加入 git 的
   untracked 檔），`git diff corpus/` 對這種檔案不會顯示任何內容，看到空 diff
   **不代表沒有東西要審閱**，只代表這個指令不適用於全新檔案；請直接打開檔案
   本身閱讀。
4. 跑 `make test` 確認通過，送 PR。
