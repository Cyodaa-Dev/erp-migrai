-- 合成語料：T100 風格的銷售訂單（單頭 + 明細）與品號主檔
-- 註：真實 T100 使用不透明的代碼式表名與欄名（如 pmaa_t / pmaa001）。
-- 本語料改用可讀名稱以便審閱；parser 不依賴命名慣例。
--
-- cust_mast 與 case01_partner 定義相同，這裡重複宣告一份。理由：載入階段
-- 必須能在單一 case 內跑完拓撲排序與外鍵解析（Task 12 的整合示範需要對
-- 單一 case 驗證），跨 case 的參照在載入時無法解析，除非兩個 case 一起
-- 載入。付出的代價是同一張表的 DDL 出現在兩個地方、改一邊要記得改另一邊；
-- 這個取捨已在 .superpowers/sdd/plan3-task-4-brief.md 記錄。
CREATE TABLE cust_mast (
    cust_no     CHAR(10)      NOT NULL,
    cust_name   VARCHAR(60)   NOT NULL,
    cust_ubn    CHAR(8),
    cust_tel    VARCHAR(20),
    cust_addr   VARCHAR(120),
    cust_credit DECIMAL(15,2) DEFAULT 0,
    cust_stus   CHAR(1)       DEFAULT 'Y',
    PRIMARY KEY (cust_no)
);

CREATE TABLE prod_mast (
    prod_no     CHAR(20)      NOT NULL,
    prod_name   VARCHAR(80)   NOT NULL,
    prod_spec   VARCHAR(120),
    prod_price  DECIMAL(15,4) DEFAULT 0,
    prod_cost   DECIMAL(15,4) DEFAULT 0,
    prod_stus   CHAR(1)       DEFAULT 'Y',
    PRIMARY KEY (prod_no)
);

CREATE TABLE ord_mast (
    ord_no      CHAR(16)      NOT NULL,
    ord_cust_no CHAR(10)      NOT NULL,
    ord_date    DATE,
    ord_cust_ref VARCHAR(40),
    ord_memo    VARCHAR(200),
    PRIMARY KEY (ord_no),
    FOREIGN KEY (ord_cust_no) REFERENCES cust_mast (cust_no)
);

-- ord_deta.ord_prod_no 刻意不宣告 FOREIGN KEY REFERENCES prod_mast (prod_no)。
-- 這是真實 T100 常見的情況：DDL 沒有 FK 約束，參照關係只存在於程式邏輯與
-- 人的知識裡，必須靠 sale_order.yaml 的規則明確宣告 reference transform 才
-- 能補上（見 src/erp_migrai/mapping/rules/sale_order.yaml 的
-- ord_deta.ord_prod_no）。ord_deta.ord_no 與 ord_mast 的關係則正常宣告 FK，
-- 用來演練「DDL 有宣告 FK、規則也宣告 reference、兩者一致」這條路。
CREATE TABLE ord_deta (
    ord_no      CHAR(16)      NOT NULL,
    ord_seq     INTEGER       NOT NULL,
    ord_prod_no CHAR(20)      NOT NULL,
    ord_qty     DECIMAL(15,4) NOT NULL,
    ord_price   DECIMAL(15,4) NOT NULL,
    ord_desc    VARCHAR(200),
    PRIMARY KEY (ord_no, ord_seq),
    FOREIGN KEY (ord_no) REFERENCES ord_mast (ord_no)
);
