"""逐句解析區塊內容。不支援的語法必須乾淨地被記錄，而不是拋出 traceback。"""

from erp_migrai.inventory.blocks import split_blocks
from erp_migrai.inventory.parser import logical_statements, parse_block, parse_definitions


def _only_block(source: str):
    result = split_blocks(source, source_file="a.4gl")
    assert len(result.blocks) == 1
    return result.blocks[0]


def test_logical_statement_starts_at_keyword_lines():
    lines = [
        (1, "    SELECT cust_no, cust_name"),
        (2, "      INTO p_no, p_name"),
        (3, "      FROM cust_mast"),
        (4, "    CALL foo()"),
    ]
    stmts = logical_statements(lines)
    assert len(stmts) == 2
    assert stmts[0][0] == 1
    assert "FROM cust_mast" in stmts[0][1]
    assert stmts[1] == (4, "CALL foo()")


def test_define_single_variable():
    defs, reason = parse_definitions("DEFINE l_cnt INTEGER", 1)
    assert reason is None
    assert [(d.name, d.type) for d in defs] == [("l_cnt", "INTEGER")]


def test_define_with_precision():
    defs, reason = parse_definitions("DEFINE l_amt DECIMAL(15,2)", 1)
    assert reason is None
    assert defs[0].type == "DECIMAL(15,2)"


def test_define_shared_type_across_names():
    defs, reason = parse_definitions("DEFINE a, b, c CHAR(10)", 3)
    assert reason is None
    assert [(d.name, d.type, d.line) for d in defs] == [
        ("a", "CHAR(10)", 3),
        ("b", "CHAR(10)", 3),
        ("c", "CHAR(10)", 3),
    ]


def test_define_record_like():
    defs, reason = parse_definitions("DEFINE p_cust RECORD LIKE cust_mast.*", 1)
    assert reason is None
    assert defs[0].type == "RECORD LIKE cust_mast.*"


def test_define_like_column():
    defs, reason = parse_definitions("DEFINE p_no LIKE cust_mast.cust_no", 1)
    assert reason is None
    assert defs[0].type == "LIKE cust_mast.cust_no"


def test_define_array():
    defs, reason = parse_definitions("DEFINE l_arr ARRAY[10] OF INTEGER", 1)
    assert reason is None
    assert defs[0].type == "ARRAY[10] OF INTEGER"


def test_unparseable_define_returns_a_reason_and_no_definitions():
    defs, reason = parse_definitions("DEFINE ))) (((", 1)
    assert defs == []
    assert reason is not None


def test_block_with_only_understood_statements_is_ok():
    block = _only_block(
        "FUNCTION f()\n"
        "    DEFINE l_cnt INTEGER\n"
        "    SELECT COUNT(*) INTO l_cnt FROM cust_mast\n"
        "    CALL g()\n"
        "END FUNCTION\n"
    )
    unit = parse_block(block)
    assert unit.kind == "FUNCTION"
    assert unit.name == "f"
    assert unit.parse_status == "ok"
    assert [d.name for d in unit.definitions] == ["l_cnt"]
    assert [(s.kind, s.tables) for s in unit.sql] == [("SELECT", ["cust_mast"])]
    assert unit.unsupported == []


def test_unknown_statement_is_recorded_and_block_becomes_partial():
    """TRY 不認得所以回報；END TRY 的首字 END 在 KNOWN_IGNORED，所以不重複回報。

    一處落差回報一次就夠——同一個缺口報兩次只會讓不支援清單失去可讀性。
    """
    block = _only_block(
        "FUNCTION f()\n    DEFINE l_cnt INTEGER\n    TRY\n    END TRY\nEND FUNCTION\n"
    )
    unit = parse_block(block)
    assert unit.parse_status == "partial"
    assert [u.line for u in unit.unsupported] == [3]
    assert unit.unsupported[0].reason == "無法辨識的語句關鍵字"
    assert [d.name for d in unit.definitions] == ["l_cnt"]


def test_block_where_nothing_is_understood_is_failed():
    block = _only_block("FUNCTION f()\n    TRY\n END FUNCTION\n")
    unit = parse_block(block)
    assert unit.parse_status == "failed"


def test_unterminated_block_is_failed_even_if_statements_parse():
    result = split_blocks("MAIN\n    DEFINE x INTEGER\n", source_file="a.4gl")
    unit = parse_block(result.blocks[0])
    assert unit.parse_status == "failed"


def test_control_flow_keywords_are_known_and_not_reported_unsupported():
    block = _only_block(
        "FUNCTION f()\n"
        "    IF x > 1 THEN\n"
        "        LET y = 2\n"
        "    ELSE\n"
        "        LET y = 3\n"
        "    END IF\n"
        "END FUNCTION\n"
    )
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_parse_block_never_raises_on_garbage():
    block = _only_block("FUNCTION f()\n    \x00\x01)))\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.parse_status in ("partial", "failed")


def test_multiline_define_record_is_one_statement_and_parses():
    """已知風險：`DEFINE ... RECORD` 的欄位常常跨好幾行寫，而 `END`（收尾用的
    `END RECORD`）本身在 KNOWN_IGNORED 裡。若天真套用「首字是已知語句關鍵字就
    開新語句」，`END RECORD` 那一行會把 DEFINE 從中間切斷，讓文法解析失敗。"""
    block = _only_block(
        "FUNCTION f()\n"
        "    DEFINE rec RECORD\n"
        "        f1 INTEGER,\n"
        "        f2 CHAR(10)\n"
        "    END RECORD\n"
        "END FUNCTION\n"
    )
    unit = parse_block(block)
    assert unit.parse_status == "ok"
    assert unit.unsupported == []
    assert [(d.name, d.type) for d in unit.definitions] == [
        ("rec", "RECORD(f1 INTEGER, f2 CHAR(10))")
    ]


def test_unrecognized_keyword_after_a_complete_statement_is_not_merged_into_it():
    """這是 TDD 過程中發現的、brief 原始程式碼本身的落差：若無法辨識的關鍵字
    一律併入前一句，`DEFINE l_cnt INTEGER` 後面接一行看不懂的 `TRY` 會被黏成
    `"DEFINE l_cnt INTEGER TRY"` 去餵文法，得到一個指向錯誤行號、講錯原因的
    失敗回報，`l_cnt` 這個定義也會憑空消失。"""
    defs, reason = parse_definitions("DEFINE l_cnt INTEGER TRY", 1)
    assert defs == []
    assert reason is not None


def test_call_with_multiline_argument_list_is_not_falsely_reported_unsupported():
    """多行 `CALL foo(a,\\n b)` 是常見寫法：括號還沒配平之前，不管下一行第一個字
    是什麼都該視為延續，不能被誤報成一句看不懂的語句。"""
    block = _only_block("FUNCTION f()\n    CALL foo(a,\n        b)\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


# --- Task 5 Resolution 2：多行 SQL 不能被子句關鍵字切碎 ------------------------


def test_order_by_clause_does_not_fragment_a_multiline_select():
    """ORDER 在 KNOWN_IGNORED 裡，但在 SQL 語句累積中不該被當成新語句的開頭。"""
    block = _only_block(
        "FUNCTION f()\n"
        "    SELECT cust_no, cust_name\n"
        "      INTO p_no, p_name\n"
        "      FROM cust_mast\n"
        "     WHERE cust_stus = 'Y'\n"
        "     ORDER BY cust_no\n"
        "END FUNCTION\n"
    )
    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [("SELECT", ["cust_mast"])]
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_declare_cursor_for_select_on_next_line_is_one_statement():
    """DECLARE ... CURSOR FOR 換行後接 SELECT，兩行要收攏成同一句 DECLARE_CURSOR，
    而不是讓 SELECT 另起一句、導致 DECLARE_CURSOR 的 tables 變成空清單。"""
    block = _only_block(
        "FUNCTION f()\n    DECLARE c1 CURSOR FOR\n    SELECT * FROM cust_mast\nEND FUNCTION\n"
    )
    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [("DECLARE_CURSOR", ["cust_mast"])]
    assert unit.unsupported == []


def test_two_adjacent_select_statements_are_not_merged():
    """緊鄰的兩個獨立 SELECT 不該被 '... FOR' 規則誤併成一句——那個規則只能在
    上一句以 FOR 結尾（DECLARE CURSOR FOR 的形狀）時才生效。"""
    block = _only_block(
        "FUNCTION f()\n    SELECT a INTO x FROM t1\n    SELECT b INTO y FROM t2\nEND FUNCTION\n"
    )
    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [
        ("SELECT", ["t1"]),
        ("SELECT", ["t2"]),
    ]


# --- Task 5 Resolution 1：parser 必須用 masked_lines，不能被註解／字串誤導 ------


def test_comment_containing_a_keyword_on_its_own_line_does_not_start_a_new_statement():
    """行內註解整段遮罩後是空白行，會被整行略過；若誤用原始文字判斷，
    `# SELECT ...` 這種註解不會是空白行，可能被誤判成另一句語句的開頭。
    這裡用一個跨行的 `CALL foo(a,` / `b)` 驗證中間夾一行整行註解不會讓
    左右兩段被錯誤地拆成兩句語句。"""
    block = _only_block(
        "FUNCTION f()\n"
        "    CALL foo(a,\n"
        "        # SELECT this looks like sql but it is a comment\n"
        "        b)\n"
        "END FUNCTION\n"
    )
    stmts = logical_statements(block.masked_lines)
    assert len(stmts) == 1
    assert stmts[0][0] == 2


def test_keyword_inside_string_literal_on_continuation_line_does_not_start_new_statement():
    """字串常值裡的 `SELECT` 只是資料，不是語句關鍵字。如果誤用原始文字的第一個字
    判斷語句邊界：字串常值前面接的是 `WHERE ... =` 這種還在累積中的 SQL 語句，
    續行的第一個字（遮罩後）應該是空字串而非 `SELECT`，因此仍會被視為這句 SQL
    的延續；若誤用原始（未遮罩）文字，字串裡的 `SELECT` 會被誤判成一句新的
    SELECT 語句的開頭，讓這句 WHERE 子句提前結束、且冒出一句荒謬的第二個
    SqlStatement。"""
    block = _only_block(
        "FUNCTION f()\n"
        "    SELECT cust_no\n"
        "      FROM cust_mast\n"
        "     WHERE cust_name =\n"
        '       "SELECT is not sql, just data"\n'
        "END FUNCTION\n"
    )
    stmts = logical_statements(block.masked_lines)
    assert len(stmts) == 1
    assert stmts[0][0] == 2

    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [("SELECT", ["cust_mast"])]
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


# --- Task 5 Fix round 1 / Defect 1：FOR 字尾判斷需要字界 ------------------------


def test_identifier_ending_in_for_does_not_falsely_continue_as_cursor_select():
    """`mgr_for` 這種以 for 結尾的識別字（ERP schema 裡常見）不該被誤判成
    `DECLARE ... CURSOR FOR` 的延續訊號；否則下一句獨立的 SELECT 會被錯誤地併入
    前一句、整句憑空消失，且不會留下任何 unsupported 紀錄——這正是本專案不能
    接受的「安靜地產出錯誤結果」。"""
    block = _only_block(
        "FUNCTION f()\n"
        "    SELECT emp_no FROM tbl WHERE dept = mgr_for\n"
        "    SELECT y FROM t2\n"
        "END FUNCTION\n"
    )
    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [
        ("SELECT", ["tbl"]),
        ("SELECT", ["t2"]),
    ]
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_declare_cursor_for_guard_still_works_after_word_boundary_fix():
    """修正字界判斷後，真正的 `DECLARE c1 CURSOR FOR` 換行接 `SELECT` 仍要收攏
    成同一句 DECLARE_CURSOR，不能因為改成字界比對而連真正的形狀都認不出來。"""
    block = _only_block(
        "FUNCTION f()\n    DECLARE c1 CURSOR FOR\n    SELECT * FROM cust_mast\nEND FUNCTION\n"
    )
    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [("DECLARE_CURSOR", ["cust_mast"])]
    assert unit.unsupported == []


# --- Task 5 Fix round 1 / Defect 2：括號未配平不能悄悄吞掉整個區塊 --------------


def test_unbalanced_paren_ends_statement_at_next_keyword_line_and_is_reported():
    """`CALL foo(a,` 的左括號永遠沒有配對到右括號時，不能把後面所有行（包含
    `DEFINE` 與 `SELECT`）一路吞到區塊結尾又完全不留痕跡——`CALL` 在
    KNOWN_IGNORED 裡，若吞掉之後沒人再檢查那句合併文字，落差會零聲張地消失。
    後續行必須各自被回收成獨立語句，且 CALL 那句要留下一筆未配平括號的
    anomaly，`parse_status` 不能是 `ok`。"""
    block = _only_block(
        "FUNCTION f()\n"
        "    CALL foo(a,\n"
        "    DEFINE l_cnt INTEGER\n"
        "    SELECT x FROM t\n"
        "END FUNCTION\n"
    )
    unit = parse_block(block)
    assert [d.name for d in unit.definitions] == ["l_cnt"]
    assert [(s.kind, s.tables) for s in unit.sql] == [("SELECT", ["t"])]
    assert len(unit.unsupported) == 1
    assert unit.unsupported[0].line == 2
    assert "括號" in unit.unsupported[0].reason
    assert unit.parse_status != "ok"


def test_defect2_guard_balanced_multiline_call_has_no_anomaly():
    """配平的多行 `CALL foo(a,` / `b)` 必須維持原本行為：一句、零 anomaly。"""
    block = _only_block("FUNCTION f()\n    CALL foo(a,\n        b)\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_unclosed_define_record_ends_at_next_keyword_line_and_is_reported():
    """`DEFINE ... RECORD` 若始終等不到 `END RECORD`，不能把後面整個區塊
    （包含後面的 `SELECT`）悄悄吞掉；必須在下一個語句關鍵字行結束，並回報
    RECORD 沒有正常關閉。"""
    block = _only_block(
        "FUNCTION f()\n"
        "    DEFINE rec RECORD\n"
        "        f1 INTEGER,\n"
        "    SELECT x FROM t\n"
        "END FUNCTION\n"
    )
    unit = parse_block(block)
    assert unit.definitions == []
    assert [(s.kind, s.tables) for s in unit.sql] == [("SELECT", ["t"])]
    assert len(unit.unsupported) == 1
    assert unit.unsupported[0].line == 2
    assert "RECORD" in unit.unsupported[0].reason
    assert unit.parse_status != "ok"


def test_unbalanced_paren_as_last_statement_reports_one_anomaly_without_crashing():
    """未配平的括號若剛好是區塊裡最後一句（後面沒有其他行可以觸發收尾），
    仍必須在區塊結尾正常收斂成一筆 anomaly，不能拋出例外或整個吞掉不報。"""
    block = _only_block("FUNCTION f()\n    CALL foo(a,\nEND FUNCTION\n")
    unit = parse_block(block)
    assert len(unit.unsupported) == 1
    assert unit.unsupported[0].line == 2
    assert "括號" in unit.unsupported[0].reason


# --- Task 5 Fix round 2 / Defect 3：SQL 續行判斷不能用「猜」的 ------------------
#
# 舊規則：SQL 累積中，只要下一行第一個字不在 _STATEMENT_KEYWORDS 就視為延續。
# 這是個猜測——COMMIT／ROLLBACK／EXECUTE 這些交易與動態 SQL 語句，一旦接在
# 一句已經完整的 SQL 語句後面，會被這個猜測誤判成延續而被吞掉，且不留下任何
# unsupported 紀錄。修正後改用「累積文字結尾是否語法上明顯沒寫完」這個真正的
# 訊號（逗號／左括號／運算子／AND OR NOT IN LIKE BETWEEN IS AS BY 這些字結尾）。


def test_commit_after_update_is_a_separate_recognized_statement():
    """回歸測試（本次修正的缺陷本身）：UPDATE ... WHERE cust_no = 5 完整結束後，
    COMMIT 不能被舊的猜測式 catch-all 併入前一句 SQL 而憑空消失。COMMIT 現在也
    要被 KNOWN_IGNORED 認得，而不是被回報成無法辨識。"""
    block = _only_block(
        "FUNCTION f()\n    UPDATE cust_mast SET x = 1 WHERE cust_no = 5\n    COMMIT\nEND FUNCTION\n"
    )
    stmts = logical_statements(block.masked_lines)
    assert len(stmts) == 2
    assert stmts[1] == (3, "COMMIT")

    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [("UPDATE", ["cust_mast"])]
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_execute_after_select_is_a_separate_statement():
    """同一種缺陷的第二個形狀：EXECUTE 接在完整的 SELECT 後面，一樣不能被吞掉。"""
    block = _only_block("FUNCTION f()\n    SELECT a FROM t\n    EXECUTE some_stmt\nEND FUNCTION\n")
    stmts = logical_statements(block.masked_lines)
    assert len(stmts) == 2
    assert stmts[1] == (3, "EXECUTE some_stmt")

    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [("SELECT", ["t"])]
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_multiline_select_with_into_from_where_and_is_one_statement():
    """五行的多行 SELECT（逗號延續欄位清單 + INTO／FROM／WHERE／AND 全部是明確的
    子句延續關鍵字）必須仍然收攏成一句，且沒有任何 anomaly。"""
    block = _only_block(
        "FUNCTION f()\n"
        "    SELECT cust_no,\n"
        "           cust_name\n"
        "      INTO p_no, p_name\n"
        "      FROM cust_mast\n"
        "     WHERE cust_stus = 'Y'\n"
        "       AND cust_credit > 0\n"
        "END FUNCTION\n"
    )
    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [("SELECT", ["cust_mast"])]
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_trailing_comma_continuation_before_reaching_a_clause_keyword():
    """SELECT 欄位清單裡，逗號結尾後面接的是另一個純欄位名稱（本身不落在任何
    關鍵字集合裡），必須靠「結尾是逗號」這個語法未完成訊號才能正確視為延續，
    而不是靠已經被移除的猜測式 catch-all。"""
    block = _only_block(
        "FUNCTION f()\n    SELECT cust_no,\n           cust_name FROM cust_mast\nEND FUNCTION\n"
    )
    stmts = logical_statements(block.masked_lines)
    assert len(stmts) == 1

    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [("SELECT", ["cust_mast"])]
    assert unit.unsupported == []


def test_statement_ending_with_trailing_comma_at_block_end_is_reported_incomplete():
    """一句語句撐到區塊結尾，結尾仍是逗號：這代表原始碼很可能漏寫了後續內容，
    必須誠實回報一筆 anomaly，而不是被靜默接受成 parse_status == 'ok'。"""
    block = _only_block("FUNCTION f()\n    SELECT cust_no,\nEND FUNCTION\n")
    unit = parse_block(block)
    assert len(unit.unsupported) == 1
    assert unit.unsupported[0].line == 2
    assert "沒寫完" in unit.unsupported[0].reason
    assert "," in unit.unsupported[0].reason
    assert unit.parse_status != "ok"


def test_update_set_still_parses_as_update_with_table():
    """守住 SET 加入 KNOWN_IGNORED 之後，UPDATE ... SET ... 不能被誤判——SET 在
    _SQL_CLAUSE_CONTINUATIONS 裡優先被檢查，所以這裡不該受影響。"""
    block = _only_block("FUNCTION f()\n    UPDATE t SET a = 1\nEND FUNCTION\n")
    unit = parse_block(block)
    assert [(s.kind, s.tables) for s in unit.sql] == [("UPDATE", ["t"])]
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_standalone_set_isolation_is_recognized_not_unknown():
    """獨立的 SET ISOLATION TO ... 語句（不是接在 UPDATE 後面的 SQL 子句）現在
    要被 KNOWN_IGNORED 認得，而不是回報成無法辨識的語句關鍵字。"""
    block = _only_block("FUNCTION f()\n    SET ISOLATION TO DIRTY READ\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


# --- Task 5 Fix round 3：結尾語法未完成的回報只能套用在 SQL 語句上 --------------
#
# `_statement_looks_incomplete` 的訊號（結尾是逗號／左括號／運算子／AND OR NOT
# 這些字）在「延續」用途上正確地只限 SQL 語句累積中才生效（`is_sql_in_progress`），
# 但「回報」用途（`_pending_group_anomaly_reason`）之前沒有做一樣的限制，導致
# `PRINT col1, col2,` 這種合法的 4GL REPORT 逗號續行慣用語被誤報成語法未完成。


def test_print_trailing_comma_report_idiom_is_not_reported_incomplete():
    """`PRINT col1, col2,` 結尾逗號是 Informix/Genero 4GL 在 REPORT 裡的合法慣用語：
    逗號會抑制換行，讓下一個 PRINT 接在同一行輸出。PRINT 不是 SQL 語句，不該被
    「結尾語法未完成」這個訊號誤報——那個訊號只對 SQL 語句才有意義。"""
    block = _only_block("FUNCTION f()\n    PRINT col1, col2,\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_print_trailing_comma_followed_by_another_print_is_two_statements_ok():
    """逗號續行的 PRINT 後面接另一個獨立的 PRINT，語句分組器正確地不合併兩者
    （逗號續行不是「延續」訊號的用途），兩句都要是零 anomaly。"""
    block = _only_block("FUNCTION f()\n    PRINT col1, col2,\n    PRINT col3, col4\nEND FUNCTION\n")
    stmts = logical_statements(block.masked_lines)
    assert len(stmts) == 2

    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_display_ending_with_trailing_comma_is_not_reported_incomplete():
    """`DISPLAY` 是另一個 KNOWN_IGNORED 的非 SQL 語句，結尾逗號同樣不該被誤報。"""
    block = _only_block("FUNCTION f()\n    DISPLAY col1, col2,\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_let_ending_with_operator_is_not_reported_incomplete():
    """`LET` 是 KNOWN_IGNORED 的非 SQL 語句，結尾是運算子（`+`）同樣不該被誤報。"""
    block = _only_block("FUNCTION f()\n    LET x = y +\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


# --- C2（branch review）：非 SQL 的多行語句續行不能被誤報 -----------------------
#
# 舊規則只在「目前正在累積一句 SQL」時才允許「結尾語法明顯未完成」這個訊號
# 觸發延續（`is_sql_clause_continuation`）。任何非 SQL 的普通 4GL 語句換行接續
# （例如 `PRINT col1,` / `col2`、`LET x = a +` / `b`），續行的第一個字不落在
# `_STATEMENT_KEYWORDS` 也不是 SQL，於是變成自己一句「無法辨識」的假語句，把
# `parse_status` 拖成 `partial`。修正後把「續行」這個判斷通用化，不再限定
# SQL：只要 (a) 續行第一個字不是已知語句關鍵字、且累積文字結尾語法明顯未完成
# （逗號／左括號／運算子／AND OR NOT IN LIKE BETWEEN IS AS BY 這些字尾），或
# (b) 續行第一個字本身就是 AND／OR／NOT，一律視為延續——這兩種訊號都與語句
# 種類無關，任何 4GL 語句都不可能以 AND／OR／NOT 開頭。「回報」（reporting）
# 用途維持原樣，仍然只在 SQL 語句才檢查「結尾語法未完成」，PRINT／DISPLAY 這類
# 合法的逗號續行慣用語不受影響（見上面 Task 5 Fix round 3 的說明）。


def test_multiline_print_continuation_is_not_falsely_reported():
    """`PRINT col1,` 換行接 `col2`：PRINT 不是 SQL，但續行本身仍是語法上明顯
    未完成（逗號結尾），必須視為延續，而不是把 `col2` 誤報成一句無法辨識的
    語句。"""
    block = _only_block("FUNCTION f()\n    PRINT col1,\n          col2\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_multiline_let_expression_continuation_is_not_falsely_reported():
    """`LET x = a +` 換行接 `b`：運算子結尾同樣是「明顯未完成」的通用訊號，
    不限 SQL。"""
    block = _only_block("FUNCTION f()\n    LET x = a +\n            b\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_multiline_if_condition_with_or_continuation_is_not_falsely_reported():
    """`IF a IS NULL` 換行接 `OR b IS NULL THEN`：這句結尾是 `NULL`，沒有觸發
    「結尾語法未完成」的 token，必須靠規則 (b)——續行第一個字是 AND／OR／NOT
    ——才能正確視為延續；沒有任何 4GL 語句以 OR 開頭，這條規則永遠安全。"""
    block = _only_block(
        "FUNCTION f()\n    IF a IS NULL\n       OR b IS NULL THEN\n    END IF\nEND FUNCTION\n"
    )
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_multiline_while_condition_with_and_continuation_is_not_falsely_reported():
    """`WHILE a > 0` 換行接 `AND b < 9`：同樣靠規則 (b)（AND／OR／NOT 開頭）
    才能正確延續，而不是把 `AND b < 9` 誤報成無法辨識的語句。"""
    block = _only_block("FUNCTION f()\n    WHILE a > 0\n          AND b < 9\nEND FUNCTION\n")
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_multiline_display_string_literal_continuation_is_not_falsely_reported():
    """`DISPLAY "a",` 換行接 `"b"`：續行遮罩後只剩一對引號（內容被遮罩成空白），
    第一個字（去除非英數字元後）是空字串，本來就不是已知語句關鍵字，且累積
    文字結尾仍是逗號（規則 a），所以應該延續成同一句 DISPLAY，不產生任何
    anomaly。這裡刻意不斷言分組後的語句數量（遮罩後的確切分行方式是實作
    細節），只斷言使用者能感知到的正確結果：零 anomaly、parse_status 為
    ok。"""
    block = _only_block('FUNCTION f()\n    DISPLAY "a",\n            "b"\nEND FUNCTION\n')
    unit = parse_block(block)
    assert unit.unsupported == []
    assert unit.parse_status == "ok"


def test_stray_closing_paren_is_reported_without_absorption_or_crash():
    """左括號短少（多寫了一個右括號）過去完全不會被回報——舊的檢查只在
    paren_balance > 0 時觸發。這裡驗證：負的括號差也要回報一筆 anomaly，且
    不能因此吞掉後面的 DEFINE，也不能拋出例外。"""
    block = _only_block("FUNCTION f()\n    CALL foo)\n    DEFINE l_cnt INTEGER\nEND FUNCTION\n")
    unit = parse_block(block)
    assert len(unit.unsupported) == 1
    assert unit.unsupported[0].line == 2
    assert "括號" in unit.unsupported[0].reason
    assert [d.name for d in unit.definitions] == ["l_cnt"]
    assert unit.parse_status != "ok"
