-- 合成來源資料。二進位的 data.sqlite 不進 git（無法在 PR 中審閱），
-- 由本檔與 schema.sql 經 `make corpus` 生成。
--
-- cust_mast 與 case01_partner/data/seed.sql 資料相同，方便交叉比對。
INSERT INTO cust_mast VALUES ('C001', '台南機械股份有限公司', '12345678', '06-1234567', '台南市安平區工業一路 1 號', 500000.00, 'Y');
INSERT INTO cust_mast VALUES ('C002', '高雄鋼鐵企業社',       '23456789', '07-2345678', '高雄市前鎮區成功二路 22 號', 300000.00, 'Y');
INSERT INTO cust_mast VALUES ('C003', '嘉義精密工業有限公司', '34567890', '05-3456789', '嘉義市西區博愛路 333 號',   0.00,      'N');

-- 品號主檔：3 筆，P003 刻意 prod_stus = 'N'，驗 value_map 走 false 分支
-- （product.product.active = false）。
INSERT INTO prod_mast VALUES ('P001', 'CNC 車床刀具', 'HSS 材質 / 20mm 柄徑', 1000.0000, 600.0000,  'Y');
INSERT INTO prod_mast VALUES ('P002', '液壓快接接頭', '3/8 吋 / 不鏽鋼',      250.0000,  150.0000,  'Y');
INSERT INTO prod_mast VALUES ('P003', '備用馬達（停產）', '3HP / 三相 220V',   5000.0000, 3000.0000, 'N');

-- 訂單單頭：2 筆，分屬不同客戶。
INSERT INTO ord_mast VALUES ('O0001', 'C001', '2026-01-05', 'PO-C001-0105', '客戶指定分批出貨');
INSERT INTO ord_mast VALUES ('O0002', 'C002', '2026-01-06', 'PO-C002-0106', NULL);

-- 訂單明細：5 筆，跨兩張訂單（O0001 三行、O0002 兩行，皆 >= 2 行，證明
-- one2many 分組不是只有一行的巧合）。ord_qty × ord_price 的合計刻意取整數，
-- 方便人工核對（見 README.md「金額手算」一節），唯獨 O0001 第 3 行刻意例外：
--   O0001：10 × 1000.0000 = 10000.0000；4 × 250.0000 = 1000.0000；
--          3 × 333.3300 = 999.9900 → 小計 11999.9900
--   O0002：5 × 1000.0000 = 5000.0000；2 × 5000.0000 = 10000.0000 → 小計 15000.0000
INSERT INTO ord_deta VALUES ('O0001', 1, 'P001', 10.0000, 1000.0000, 'CNC 車床刀具 10 支');
INSERT INTO ord_deta VALUES ('O0001', 2, 'P002', 4.0000,  250.0000,  '液壓快接接頭 4 個');
-- 第 3 行單價 333.3300 刻意帶小數尾數，讓 Task 11 若誤寫成 Decimal(v)
-- （直接吃 SQLite 讀回的 float）而非 Decimal(str(v))，兩者結果不同：
-- SQLite 對 DECIMAL(15,4) 的 NUMERIC 親和性在值有非零小數部分時會存成
-- IEEE 754 float，Python 讀回是 333.33 這個 float，Decimal(333.33) ==
-- 333.32999999999998408384271897375583648681640625，Decimal('333.33') ==
-- 333.33，兩者不相等（實測見 .superpowers/sdd/plan3-task-5-report.md）。
INSERT INTO ord_deta VALUES ('O0001', 3, 'P002', 3.0000,  333.3300,  '液壓快接接頭 3 個（促銷折讓後單價，非整數）');
INSERT INTO ord_deta VALUES ('O0002', 1, 'P001', 5.0000,  1000.0000, 'CNC 車床刀具 5 支');
INSERT INTO ord_deta VALUES ('O0002', 2, 'P003', 2.0000,  5000.0000, '備用馬達 2 台');
