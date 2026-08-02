# 合成語料：銷售訂單雙檔維護程式（單頭 ord_mast + 明細 ord_deta）
# 展示語法：DATABASE / GLOBALS / MAIN / FUNCTION / DEFINE（含 RECORD LIKE）/
#           內嵌 SELECT、INSERT、UPDATE、DELETE / DECLARE ... CURSOR FOR + FOREACH /
#           MENU / INPUT
#
# 刻意混入 3 條已知不支援的 Genero 語法（詳見 README.md「不支援語法」一節，
# 每一條 scan 時都會各自產生一筆 unsupported_syntax、reason 為「無法辨識的
# 語句關鍵字」，並讓所屬 FUNCTION 的 parse_status 變成 partial）：
#   - TRY（例外處理起始）
#   - CATCH（例外處理分支）
#   - DEFER INTERRUPT（中斷延遲）
# 註：END TRY 本身不會被記成不支援——首字 END 在 KNOWN_IGNORED 之列，parser
#     不深究 END 後面接的是哪一種區塊關鍵字。
DATABASE ds

GLOBALS
    DEFINE g_user CHAR(10)
END GLOBALS

MAIN
    LET g_user = "admin"
    CALL so_maintain()
END MAIN

FUNCTION so_maintain()
    DEFINE l_cnt INTEGER

    SELECT COUNT(*) INTO l_cnt FROM ord_mast

    MENU "銷售訂單維護"
        COMMAND "新增"
            CALL so_insert()
        COMMAND "修改"
            CALL so_update()
        COMMAND "刪除"
            CALL so_delete()
        COMMAND "明細清單"
            CALL so_list_deta()
        COMMAND "離開"
            EXIT MENU
    END MENU
END FUNCTION

FUNCTION so_insert()
    -- 雙檔維護寫入期間不希望使用者用 CTRL-C 之類的中斷鍵打斷單頭／明細兩張表
    -- 的寫入順序，真實 T100 常見這個寫法；本工具目前不支援解析它。
    DEFER INTERRUPT

    DEFINE p_ord  RECORD LIKE ord_mast.*
    DEFINE p_deta RECORD LIKE ord_deta.*

    INITIALIZE p_ord.* TO NULL
    INPUT BY NAME p_ord.* WITHOUT DEFAULTS
    INSERT INTO ord_mast VALUES (p_ord.*)

    INITIALIZE p_deta.* TO NULL
    INPUT BY NAME p_deta.* WITHOUT DEFAULTS
    INSERT INTO ord_deta VALUES (p_deta.*)
END FUNCTION

FUNCTION so_update()
    DEFINE p_no  LIKE ord_mast.ord_no
    DEFINE p_ord RECORD LIKE ord_mast.*

    SELECT ord_no, ord_cust_no, ord_date, ord_cust_ref, ord_memo
      INTO p_ord.*
      FROM ord_mast
     WHERE ord_no = p_no

    INPUT BY NAME p_ord.* WITHOUT DEFAULTS
    UPDATE ord_mast SET ord_memo = p_ord.ord_memo WHERE ord_no = p_no
END FUNCTION

FUNCTION so_delete()
    DEFINE p_no LIKE ord_mast.ord_no

    -- 明細必須先刪，單頭後刪，避免違反 ord_deta.ord_no 對 ord_mast 的外鍵。
    DELETE FROM ord_deta WHERE ord_no = p_no
    DELETE FROM ord_mast WHERE ord_no = p_no
END FUNCTION

FUNCTION so_list_deta()
    DEFINE p_no   LIKE ord_mast.ord_no
    DEFINE p_deta RECORD LIKE ord_deta.*

    DECLARE c_deta CURSOR FOR
    SELECT ord_no, ord_seq, ord_prod_no, ord_qty, ord_price, ord_desc
      FROM ord_deta
     WHERE ord_no = p_no

    FOREACH c_deta INTO p_deta.*
        DISPLAY p_deta.ord_prod_no
    END FOREACH

    -- 例外處理：本工具目前不解析 TRY / CATCH / END TRY，三行各自的落差都會
    -- 被誠實回報（見檔頭與 README.md）。
    TRY
        CALL so_recalc_total(p_no)
    CATCH
        DISPLAY "so_recalc_total 失敗"
    END TRY
END FUNCTION
