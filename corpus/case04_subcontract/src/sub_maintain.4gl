# 合成語料：委外加工發包雙檔維護程式（單頭 sub_ord + 單身 sub_deta）
# 展示語法：DATABASE / GLOBALS / MAIN / FUNCTION / DEFINE（含 RECORD LIKE）/
#           內嵌 SELECT、INSERT、UPDATE、DELETE / DECLARE ... CURSOR FOR + FOREACH /
#           MENU / INPUT
#
# 刻意混入 2 條已知不支援的 Genero 語法（詳見 README.md「不支援語法」一節，
# scan 時各自產生一筆 unsupported_syntax，並讓所屬 FUNCTION 的 parse_status
# 變成 partial）：
#   - WHENEVER ERROR CONTINUE（錯誤處理指示）
#   - DEFER INTERRUPT（中斷延遲）
DATABASE ds

GLOBALS
    DEFINE g_user CHAR(10)
    DEFINE g_plant CHAR(4)
END GLOBALS

MAIN
    LET g_user = "admin"
    LET g_plant = "MW"
    CALL sub_maintain()
END MAIN

FUNCTION sub_maintain()
    DEFINE l_cnt INTEGER

    SELECT COUNT(*) INTO l_cnt FROM sub_ord

    MENU "委外發包維護"
        COMMAND "新增"
            CALL sub_insert()
        COMMAND "修改回廠日"
            CALL sub_update_eta()
        COMMAND "刪除"
            CALL sub_delete()
        COMMAND "未回廠清單"
            CALL sub_list_open()
        COMMAND "離開"
            EXIT MENU
    END MENU
END FUNCTION

FUNCTION sub_insert()
    -- 單頭與單身要一起寫入，寫入期間不接受中斷
    DEFER INTERRUPT

    DEFINE l_ord RECORD LIKE sub_ord.*
    DEFINE l_det RECORD LIKE sub_deta.*

    INPUT BY NAME l_ord.sub_no, l_ord.sub_vend_no, l_ord.sub_wo_no, l_ord.sub_date

    INSERT INTO sub_ord (sub_no, sub_vend_no, sub_wo_no, sub_date, sub_memo)
        VALUES (l_ord.sub_no, l_ord.sub_vend_no, l_ord.sub_wo_no, l_ord.sub_date, l_ord.sub_memo)

    INPUT BY NAME l_det.sub_item_no, l_det.sub_wc_no, l_det.sub_qty, l_det.sub_price, l_det.sub_eta

    INSERT INTO sub_deta (sub_no, sub_seq, sub_item_no, sub_wc_no, sub_qty, sub_price, sub_eta, sub_trial)
        VALUES (l_ord.sub_no, l_det.sub_seq, l_det.sub_item_no, l_det.sub_wc_no,
                l_det.sub_qty, l_det.sub_price, l_det.sub_eta, l_det.sub_trial)
END FUNCTION

FUNCTION sub_update_eta()
    -- 回廠日是自由文字，現場常直接覆寫成「延到下週」這類字串
    DEFINE l_no   CHAR(16)
    DEFINE l_seq  SMALLINT
    DEFINE l_eta  VARCHAR(40)

    INPUT BY NAME l_no, l_seq, l_eta

    UPDATE sub_deta
        SET sub_eta = l_eta
        WHERE sub_no = l_no AND sub_seq = l_seq
END FUNCTION

FUNCTION sub_delete()
    DEFINE l_no CHAR(16)

    INPUT BY NAME l_no

    DELETE FROM sub_deta WHERE sub_no = l_no
    DELETE FROM sub_ord  WHERE sub_no = l_no
END FUNCTION

FUNCTION sub_list_open()
    -- 未回廠清單：委外工序（wc_outs = 'Y'）且尚未結案的單身
    WHENEVER ERROR CONTINUE

    DEFINE l_no    CHAR(16)
    DEFINE l_item  CHAR(20)
    DEFINE l_wc    CHAR(12)
    DEFINE l_eta   VARCHAR(40)
    DEFINE l_vend  VARCHAR(60)

    DECLARE c_open CURSOR FOR
        SELECT d.sub_no, d.sub_item_no, d.sub_wc_no, d.sub_eta, v.vend_name
            FROM sub_deta d, sub_ord o, vend_mast v, wc_mast w
            WHERE d.sub_no = o.sub_no
              AND o.sub_vend_no = v.vend_no
              AND d.sub_wc_no = w.wc_no
              AND w.wc_outs = 'Y'

    FOREACH c_open INTO l_no, l_item, l_wc, l_eta, l_vend
        DISPLAY l_no, l_item, l_wc, l_eta, l_vend
    END FOREACH
END FUNCTION
