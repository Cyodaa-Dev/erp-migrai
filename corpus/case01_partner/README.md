# case01_partner — 客戶主檔單檔維護

**合成語料**（`src/SYNTHETIC`）。依 Genero BDL 公開語法規格自建，非任何客戶之真實程式碼。

## 這個 case 展示什麼

| 面向 | 內容 |
|---|---|
| 4GL 語法 | `DATABASE`、`GLOBALS`、`MAIN`、`FUNCTION`、`MENU`／`COMMAND`、`INPUT` |
| 宣告 | `CHAR(n)`、`INTEGER`、`RECORD LIKE <table>.*`、`LIKE <table>.<column>` |
| 內嵌 SQL | `SELECT ... INTO ... FROM`、`INSERT INTO`、`UPDATE ... SET`、`DELETE FROM` |
| 來源結構 | 單一主檔，單欄主鍵，含 `DECIMAL(15,2)` 與 `DEFAULT` |
| 目標 Odoo 模型 | `res.partner`（於 map 階段與載入階段使用） |

## 這個 case 不含什麼

- 沒有 `CURSOR`／`FOREACH`——未來新增的 case 會補上
- 沒有單位轉換——未來新增的 case 會補上
- **沒有不支援的語法**——本 case 應該全數解析成功，`parse_status` 全為 `ok`

## 檔案

| 檔案 | 用途 |
|---|---|
| `src/cust_maintain.4gl` | 合成 Genero 程式 |
| `src/schema.sql` | T100 風格來源表 DDL |
| `data/seed.sql` | 合成來源資料。`data.sqlite` 不進 git，由 `make corpus` 生成 |
| `expected/inventory.json` | golden：`migrai scan` 應該吐出的內容 |
| `expected/mapping.json` | golden：`migrai map` 應該吐出的內容 |
