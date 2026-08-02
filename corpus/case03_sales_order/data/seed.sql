-- 合成來源資料。二進位的 data.sqlite 不進 git（無法在 PR 中審閱），
-- 由本檔與 schema.sql 在載入階段生成。
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

-- 訂單明細：4 筆，跨兩張訂單，每張訂單各 2 行（皆 >= 2 行，證明 one2many
-- 分組不是只有一行的巧合）。ord_qty × ord_price 的合計刻意取整數，方便人工
-- 核對（見 README.md「金額手算」一節）：
--   O0001：10 × 1000.0000 = 10000.0000；4 × 250.0000 = 1000.0000 → 小計 11000.0000
--   O0002：5 × 1000.0000 = 5000.0000；2 × 5000.0000 = 10000.0000 → 小計 15000.0000
INSERT INTO ord_deta VALUES ('O0001', 1, 'P001', 10.0000, 1000.0000, 'CNC 車床刀具 10 支');
INSERT INTO ord_deta VALUES ('O0001', 2, 'P002', 4.0000,  250.0000,  '液壓快接接頭 4 個');
INSERT INTO ord_deta VALUES ('O0002', 1, 'P001', 5.0000,  1000.0000, 'CNC 車床刀具 5 支');
INSERT INTO ord_deta VALUES ('O0002', 2, 'P003', 2.0000,  5000.0000, '備用馬達 2 台');
