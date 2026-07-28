# 合成語料：客戶主檔單檔維護程式
# 展示語法：DATABASE / GLOBALS / MAIN / FUNCTION / DEFINE（含 RECORD LIKE）/
#           內嵌 SELECT、INSERT、UPDATE、DELETE / MENU / INPUT
DATABASE ds

GLOBALS
    DEFINE g_user CHAR(10)
END GLOBALS

MAIN
    LET g_user = "admin"
    CALL cust_maintain()
END MAIN

FUNCTION cust_maintain()
    DEFINE l_cnt INTEGER

    SELECT COUNT(*) INTO l_cnt FROM cust_mast

    MENU "客戶維護"
        COMMAND "新增"
            CALL cust_insert()
        COMMAND "修改"
            CALL cust_update()
        COMMAND "刪除"
            CALL cust_delete()
        COMMAND "離開"
            EXIT MENU
    END MENU
END FUNCTION

FUNCTION cust_insert()
    DEFINE p_cust RECORD LIKE cust_mast.*

    INITIALIZE p_cust.* TO NULL
    INPUT BY NAME p_cust.* WITHOUT DEFAULTS
    INSERT INTO cust_mast VALUES (p_cust.*)
END FUNCTION

FUNCTION cust_update()
    DEFINE p_no LIKE cust_mast.cust_no
    DEFINE p_cust RECORD LIKE cust_mast.*

    SELECT cust_no, cust_name, cust_ubn, cust_tel, cust_addr, cust_credit, cust_stus
      INTO p_cust.*
      FROM cust_mast
     WHERE cust_no = p_no

    INPUT BY NAME p_cust.* WITHOUT DEFAULTS
    UPDATE cust_mast SET cust_name = p_cust.cust_name WHERE cust_no = p_no
END FUNCTION

FUNCTION cust_delete()
    DEFINE p_no LIKE cust_mast.cust_no

    DELETE FROM cust_mast WHERE cust_no = p_no
END FUNCTION
