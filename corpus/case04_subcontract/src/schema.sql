-- 合成語料：T100 風格的委外加工發包（單頭 + 單身）、工作中心、品號與製令
--
-- 註：真實 T100 使用不透明的代碼式表名與欄名（如 pmaa_t / pmaa001）。
-- 本語料改用可讀名稱以便審閱；parser 不依賴命名慣例。
--
-- 主題選擇的理由：委外加工是金屬沖焊代工廠「在製品離廠後狀態不可見」的根源，
-- 也是本語料要演示的三個移轉決策點所在（見 README.md）。

-- 委外廠商主檔。與 case01/case03 的 cust_mast 是不同的表（供應商而非客戶），
-- 但同樣示範 CHAR(1) 的 Y/N 狀態欄位需要值轉換才能進 Odoo 的 boolean active。
CREATE TABLE vend_mast (
    vend_no     CHAR(10)      NOT NULL,
    vend_name   VARCHAR(60)   NOT NULL,
    vend_ubn    CHAR(8),
    vend_tel    VARCHAR(20),
    vend_addr   VARCHAR(120),
    vend_proc   VARCHAR(40),
    vend_stus   CHAR(1)       DEFAULT 'Y',
    PRIMARY KEY (vend_no)
);

-- 品號主檔。刻意包含模具件（DIE-*）——模具在來源系統有品號，
-- 但「累計衝次」與「設計壽命」兩欄在 Odoo 標準模型中沒有對應欄位。
CREATE TABLE item_mast (
    item_no     CHAR(20)      NOT NULL,
    item_name   VARCHAR(80)   NOT NULL,
    item_spec   VARCHAR(120),
    item_price  DECIMAL(15,4) DEFAULT 0,
    item_cost   DECIMAL(15,4) DEFAULT 0,
    item_kind   CHAR(2)       DEFAULT 'FG',
    item_hits   INTEGER       DEFAULT 0,
    item_life   INTEGER       DEFAULT 0,
    item_stus   CHAR(1)       DEFAULT 'Y',
    PRIMARY KEY (item_no)
);

-- 工作中心。MW- 前綴為本廠工序、OS- 前綴為委外工序；
-- 這個前綴就是「在製品是否離廠」的唯一判斷依據。
CREATE TABLE wc_mast (
    wc_no       CHAR(12)      NOT NULL,
    wc_name     VARCHAR(60)   NOT NULL,
    wc_rate     DECIMAL(15,4) DEFAULT 0,
    wc_outs     CHAR(1)       DEFAULT 'N',
    PRIMARY KEY (wc_no)
);

-- 製令主檔。
CREATE TABLE wo_mast (
    wo_no       CHAR(16)      NOT NULL,
    wo_item_no  CHAR(20)      NOT NULL,
    wo_qty      DECIMAL(15,4) DEFAULT 0,
    wo_date     DATE,
    wo_stus     CHAR(2)       DEFAULT '10',
    PRIMARY KEY (wo_no),
    FOREIGN KEY (wo_item_no) REFERENCES item_mast (item_no)
);

-- 委外發包單頭。
CREATE TABLE sub_ord (
    sub_no      CHAR(16)      NOT NULL,
    sub_vend_no CHAR(10)      NOT NULL,
    sub_wo_no   CHAR(16),
    sub_date    DATE,
    sub_memo    VARCHAR(200),
    PRIMARY KEY (sub_no),
    FOREIGN KEY (sub_vend_no) REFERENCES vend_mast (vend_no)
);

-- 委外發包單身。
-- 🔴 sub_eta 是本語料最重要的一個形狀：預計回廠日在來源系統是 VARCHAR 自由文字
--    （現場實務就是這樣填的：「約8/15」「月底前」「等對方回覆」），
--    但 Odoo 的 date_planned 是 date 型別。這一欄的搬遷決策——要不要解析、
--    解析不出來怎麼辦——就是「委外進度不可見」在資料層的根因。
CREATE TABLE sub_deta (
    sub_no      CHAR(16)      NOT NULL,
    sub_seq     SMALLINT      NOT NULL,
    sub_item_no CHAR(20)      NOT NULL,
    sub_wc_no   CHAR(12),
    sub_qty     DECIMAL(15,4) DEFAULT 0,
    sub_price   DECIMAL(15,4) DEFAULT 0,
    sub_eta     VARCHAR(40),
    sub_trial   SMALLINT      DEFAULT 0,
    PRIMARY KEY (sub_no, sub_seq),
    FOREIGN KEY (sub_no) REFERENCES sub_ord (sub_no),
    FOREIGN KEY (sub_item_no) REFERENCES item_mast (item_no)
);
