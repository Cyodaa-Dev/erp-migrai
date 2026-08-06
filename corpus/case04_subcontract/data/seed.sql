-- 合成語料：由 ACEX-MW 的 Odoo 快照反向產生（tools/gen_seed.py），勿手改。
-- 業務資料全部來自 snapshot；統編、電話、地址為合成值，非任何真實資訊。

-- 委外廠商主檔
INSERT INTO vend_mast VALUES ('VCNC000','CNC車床加工協力廠','53810000','06-2450100','臺南市工業區','CNC車床加工','Y');
INSERT INTO vend_mast VALUES ('VCOAT00','表面處理(電著/粉體塗裝)協力廠','53817311','06-2463107','臺南市工業區','表面處理(電著/粉體塗裝)','Y');
INSERT INTO vend_mast VALUES ('VFORGE0','鍛造協力廠','53824622','06-2476114','臺南市工業區','鍛造','Y');
INSERT INTO vend_mast VALUES ('VPLATE0','電鍍/鍍鉻協力廠','53831933','06-2489121','臺南市工業區','電鍍/鍍鉻','Y');
INSERT INTO vend_mast VALUES ('VRUBBER','橡膠組件協力廠','53839244','06-2502128','臺南市工業區','橡膠組件','Y');
INSERT INTO vend_mast VALUES ('VTHREAD','輪牙/攻牙協力廠','53846555','06-2515135','臺南市工業區','輪牙/攻牙','N');

-- 品號主檔：MW-*/CMP-*/RAW-* 取自 Odoo
INSERT INTO item_mast VALUES ('CMP-CNC-SHAFT','CNC車製軸件(委外回廠)',NULL,0.0,285.0,'SF',0,0,'Y');
INSERT INTO item_mast VALUES ('CMP-FORGE-JOINT','鍛造接頭(委外回廠)',NULL,0.0,210.0,'SF',0,0,'Y');
INSERT INTO item_mast VALUES ('CMP-RUBBER-BUSH','橡膠襯套(委外回廠)',NULL,0.0,45.0,'SF',0,0,'Y');
INSERT INTO item_mast VALUES ('CMP-STAMP-BRKT','沖壓支架半成品',NULL,0.0,96.0,'SF',0,0,'Y');
INSERT INTO item_mast VALUES ('CMP-STAMP-PLATE','沖壓板件半成品',NULL,0.0,74.0,'SF',0,0,'Y');
INSERT INTO item_mast VALUES ('CMP-TUBE-BEND','彎管半成品',NULL,0.0,165.0,'SF',0,0,'Y');
INSERT INTO item_mast VALUES ('MW-MB-WCHAIR','電動輪椅支架',NULL,1950.0,920.0,'FG',0,0,'Y');
INSERT INTO item_mast VALUES ('MW-MC-ENGMNT','引擎吊架',NULL,680.0,305.0,'FG',0,0,'Y');
INSERT INTO item_mast VALUES ('MW-MC-FRAME','機車車架總成',NULL,2850.0,1420.0,'FG',0,0,'Y');
INSERT INTO item_mast VALUES ('MW-MC-PEDAL','剎車踏板組',NULL,420.0,186.0,'FG',0,0,'Y');
INSERT INTO item_mast VALUES ('MW-SP-RACK','運動器材金屬骨架',NULL,1680.0,780.0,'FG',0,0,'Y');
INSERT INTO item_mast VALUES ('MW-TK-BEARING','卡車軸承架',NULL,1150.0,540.0,'FG',0,0,'Y');
INSERT INTO item_mast VALUES ('RAW-COIL','熱軋/冷軋鋼捲',NULL,0.0,28.0,'RM',0,0,'Y');
INSERT INTO item_mast VALUES ('RAW-TUBE','鋼管/型材',NULL,0.0,36.0,'RM',0,0,'Y');
-- 🔴 模具件：來源系統有品號與累計衝次/設計壽命，Odoo 標準模型沒有這兩個欄位
INSERT INTO item_mast VALUES ('DIE-2043','連續模 剎車踏板組','沖壓模具',0,0,'DI',892000,1000000,'Y');
INSERT INTO item_mast VALUES ('DIE-3117','單發模 引擎吊架','沖壓模具',0,0,'DI',214500,800000,'Y');
INSERT INTO item_mast VALUES ('DIE-4602','彎管模 電動輪椅支架','沖壓模具',0,0,'DI',63200,600000,'Y');

-- 工作中心：MW- 本廠 / OS- 委外，前綴是「在製品是否離廠」的唯一依據
INSERT INTO wc_mast VALUES ('MW-ASM','組立',560.0,'N');
INSERT INTO wc_mast VALUES ('MW-BEND','彎管成型',720.0,'N');
INSERT INTO wc_mast VALUES ('MW-CMM','三次元量測與首中末件檢驗',640.0,'N');
INSERT INTO wc_mast VALUES ('MW-PRESS4','400T油壓機',1280.0,'N');
INSERT INTO wc_mast VALUES ('MW-PRESS5','500T油壓機',1450.0,'N');
INSERT INTO wc_mast VALUES ('MW-ROBOT','機械手臂焊接(CO2/氬焊)',1150.0,'N');
INSERT INTO wc_mast VALUES ('MW-SHEAR','剪切下料',480.0,'N');
INSERT INTO wc_mast VALUES ('MW-STAMP','300T以下沖床線',860.0,'N');
INSERT INTO wc_mast VALUES ('MW-WELD','人工焊接與焊圓',680.0,'N');
INSERT INTO wc_mast VALUES ('OS-CNC','委外-CNC車床加工',620.0,'Y');
INSERT INTO wc_mast VALUES ('OS-COAT','委外-表面處理(電著/粉體塗裝)',420.0,'Y');
INSERT INTO wc_mast VALUES ('OS-FORGE','委外-鍛造',540.0,'Y');
INSERT INTO wc_mast VALUES ('OS-PLATE','委外-電鍍/鍍鉻',460.0,'Y');
INSERT INTO wc_mast VALUES ('OS-RUBBER','委外-橡膠組件',350.0,'Y');
INSERT INTO wc_mast VALUES ('OS-THREAD','委外-輪牙/攻牙',380.0,'Y');

-- 製令
INSERT INTO wo_mast VALUES ('MW/MO/00001','MW-MC-FRAME',320.0,'2026-08-10','30');
INSERT INTO wo_mast VALUES ('MW/MO/00002','MW-MC-PEDAL',1500.0,'2026-08-10','30');
INSERT INTO wo_mast VALUES ('MW/MO/00003','MW-MB-WCHAIR',260.0,'2026-09-09','30');
INSERT INTO wo_mast VALUES ('MW/MO/00004','MW-SP-RACK',180.0,'2026-08-10','20');
INSERT INTO wo_mast VALUES ('MW/MO/00005','MW-TK-BEARING',400.0,'2026-08-14','20');
INSERT INTO wo_mast VALUES ('MW/MO/00006','MW-MC-ENGMNT',900.0,'2026-08-07','20');

-- 委外發包單頭／單身
-- 🔴 sub_eta 保留現場實務的自由文字：可解析日期、模糊描述、空白三種都有
INSERT INTO sub_ord VALUES ('SUB-MW-0001','VCOAT00','MW/MO/00001','2026-07-24','委外電著塗裝');
INSERT INTO sub_deta VALUES ('SUB-MW-0001',1,'CMP-STAMP-BRKT','OS-COAT',320.0,0,'2026/08/15',0);
INSERT INTO sub_ord VALUES ('SUB-MW-0002','VTHREAD','MW/MO/00002','2026-08-01','委外輪牙');
INSERT INTO sub_deta VALUES ('SUB-MW-0002',1,'CMP-TUBE-BEND','OS-THREAD',1500.0,0,'約8月中',1);
INSERT INTO sub_ord VALUES ('SUB-MW-0003','VPLATE0','MW/MO/00002','2026-07-29','委外電鍍');
INSERT INTO sub_deta VALUES ('SUB-MW-0003',1,'CMP-STAMP-PLATE','OS-PLATE',1500.0,0,'2026/08/03',2);
INSERT INTO sub_ord VALUES ('SUB-MW-0004','VCOAT00','MW/MO/00003','2026-08-07','委外粉體塗裝');
INSERT INTO sub_deta VALUES ('SUB-MW-0004',1,'CMP-STAMP-BRKT','OS-COAT',260.0,0,'月底前',0);
INSERT INTO sub_ord VALUES ('SUB-MW-0005','VCOAT00','MW/MO/00004','2026-08-18','委外粉體塗裝');
INSERT INTO sub_deta VALUES ('SUB-MW-0005',1,'CMP-STAMP-BRKT','OS-COAT',180.0,0,'',1);
INSERT INTO sub_ord VALUES ('SUB-MW-0006','VCNC000','MW/MO/00005','2026-10-28','委外CNC車製');
INSERT INTO sub_deta VALUES ('SUB-MW-0006',1,'CMP-CNC-SHAFT','OS-CNC',400.0,0,'等對方回覆',2);
INSERT INTO sub_ord VALUES ('SUB-MW-0007','VFORGE0','MW/MO/00005','2026-08-25','委外鍛造');
INSERT INTO sub_deta VALUES ('SUB-MW-0007',1,'CMP-FORGE-JOINT','OS-FORGE',400.0,0,'2026/10/28',0);
