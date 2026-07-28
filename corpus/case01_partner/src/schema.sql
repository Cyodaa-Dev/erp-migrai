-- 合成語料：T100 風格的客戶主檔
-- 註：真實 T100 使用不透明的代碼式表名與欄名（如 pmaa_t / pmaa001）。
-- 本語料改用可讀名稱以便審閱；parser 不依賴命名慣例。
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
