"""schema.sql 的表結構擷取。"""

from erp_migrai.inventory.ddl import extract_tables, split_statements

SCHEMA = """
-- T100 風格客戶主檔
CREATE TABLE cust_mast (
    cust_no     CHAR(10)      NOT NULL,
    cust_name   VARCHAR(60)   NOT NULL,
    cust_ubn    CHAR(8),
    cust_tel    VARCHAR(20),
    cust_credit DECIMAL(15,2) DEFAULT 0,
    cust_stus   CHAR(1)       DEFAULT 'Y',
    PRIMARY KEY (cust_no)
);

CREATE TABLE cust_addr (
    addr_seq    INTEGER  NOT NULL PRIMARY KEY,
    cust_no     CHAR(10) NOT NULL,
    addr_text   VARCHAR(120),
    FOREIGN KEY (cust_no) REFERENCES cust_mast (cust_no)
);
"""


def test_split_statements_reports_one_based_start_lines():
    stmts, unsupported = split_statements(
        "CREATE TABLE a (x INT);\n\nCREATE TABLE b (y INT);\n", source_file="schema.sql"
    )
    assert [line for line, _ in stmts] == [1, 3]
    assert unsupported == []


def test_split_statements_drops_line_comments_but_keeps_line_numbers():
    stmts, unsupported = split_statements(
        "-- 註解\nCREATE TABLE a (x INT);\n", source_file="schema.sql"
    )
    assert stmts[0][0] == 2
    assert unsupported == []


def test_extracts_both_tables_sorted_by_name():
    tables, unsupported = extract_tables(SCHEMA, source_file="schema.sql")
    assert [t.name for t in tables] == ["cust_addr", "cust_mast"]
    assert unsupported == []


def test_column_types_are_normalised_uppercase_with_precision():
    tables, _ = extract_tables(SCHEMA, source_file="schema.sql")
    cust_mast = next(t for t in tables if t.name == "cust_mast")
    types = {c.name: c.type for c in cust_mast.columns}
    assert types["cust_no"] == "CHAR(10)"
    assert types["cust_credit"] == "DECIMAL(15,2)"
    assert types["cust_tel"] == "VARCHAR(20)"


def test_not_null_and_defaults_are_captured():
    tables, _ = extract_tables(SCHEMA, source_file="schema.sql")
    cust_mast = next(t for t in tables if t.name == "cust_mast")
    cols = {c.name: c for c in cust_mast.columns}
    assert cols["cust_no"].nullable is False
    assert cols["cust_tel"].nullable is True
    assert cols["cust_stus"].default == "Y"
    assert cols["cust_credit"].default == "0"


def test_table_level_primary_key():
    tables, _ = extract_tables(SCHEMA, source_file="schema.sql")
    cust_mast = next(t for t in tables if t.name == "cust_mast")
    assert cust_mast.primary_key == ["cust_no"]


def test_inline_primary_key_is_captured():
    tables, _ = extract_tables(SCHEMA, source_file="schema.sql")
    cust_addr = next(t for t in tables if t.name == "cust_addr")
    assert cust_addr.primary_key == ["addr_seq"]


def test_foreign_key_is_captured():
    tables, _ = extract_tables(SCHEMA, source_file="schema.sql")
    cust_addr = next(t for t in tables if t.name == "cust_addr")
    assert len(cust_addr.foreign_keys) == 1
    fk = cust_addr.foreign_keys[0]
    assert fk.columns == ["cust_no"]
    assert fk.ref_table == "cust_mast"
    assert fk.ref_columns == ["cust_no"]


def test_non_create_table_statement_is_recorded_not_raised():
    tables, unsupported = extract_tables(
        "CREATE INDEX idx_cust ON cust_mast (cust_no);", source_file="schema.sql"
    )
    assert tables == []
    assert len(unsupported) == 1
    assert unsupported[0].line == 1
    assert "CREATE TABLE" in unsupported[0].reason


def test_malformed_create_table_is_recorded_not_raised():
    tables, unsupported = extract_tables("CREATE TABLE broken (((;", source_file="schema.sql")
    assert tables == []
    assert len(unsupported) == 1
    assert unsupported[0].file == "schema.sql"


def test_snippets_are_truncated():
    long_stmt = "CREATE INDEX " + "x" * 500 + ";"
    _, unsupported = extract_tables(long_stmt, source_file="schema.sql")
    assert len(unsupported[0].snippet) <= 200


def test_inline_block_comment_does_not_merge_tokens():
    """單行區塊註解 /**/ 必須換成空白而非空字串，否則 token 會被黏在一起。"""
    tables, unsupported = extract_tables(
        "CREATE TABLE t (x INT/**/NOT NULL);", source_file="schema.sql"
    )
    assert unsupported == []
    col = tables[0].columns[0]
    assert col.type == "INT"
    assert col.nullable is False


def test_multiline_block_comment_keeps_later_line_numbers():
    """properly 終止的多行區塊註解不受 Rule 1（未終止才在行尾恢復）影響。"""
    text = "CREATE TABLE a (x INT);\n/* comment\nspans\nlines */\nCREATE TABLE b (y INT);\n"
    stmts, unsupported = split_statements(text, source_file="schema.sql")
    assert [line for line, _ in stmts] == [1, 5]
    assert unsupported == []


def test_semicolon_inside_quoted_string_does_not_split_statement():
    tables, unsupported = extract_tables(
        "CREATE TABLE a (x CHAR(1) DEFAULT ';');", source_file="schema.sql"
    )
    assert len(tables) == 1
    assert unsupported == []


def test_doubled_quote_inside_string_literal_is_not_a_terminator():
    text = "CREATE TABLE a (x CHAR(10) DEFAULT 'it''s ok');\nCREATE TABLE b (y INT);\n"
    stmts, unsupported = split_statements(text, source_file="schema.sql")
    assert len(stmts) == 2
    assert stmts[0][1].count("'") == 4
    assert stmts[1][0] == 2
    assert unsupported == []


def test_line_comment_marker_inside_string_literal_is_not_a_comment():
    text = "CREATE TABLE a (x CHAR(20) DEFAULT '--not a comment');\n"
    stmts, unsupported = split_statements(text, source_file="schema.sql")
    assert len(stmts) == 1
    assert "--not a comment" in stmts[0][1]
    assert unsupported == []


def test_statement_without_trailing_semicolon_is_returned():
    stmts, unsupported = split_statements("CREATE TABLE a (x INT)", source_file="schema.sql")
    assert len(stmts) == 1
    assert stmts[0][1] == "CREATE TABLE a (x INT)"
    assert unsupported == []


def test_trailing_semicolon_with_only_whitespace_after_yields_no_empty_statement():
    stmts, unsupported = split_statements(
        "CREATE TABLE a (x INT);\n   \n", source_file="schema.sql"
    )
    assert len(stmts) == 1
    assert unsupported == []


def test_unterminated_block_comment_does_not_hang_or_raise():
    tables, unsupported = extract_tables(
        "CREATE TABLE a (x INT/* never closed", source_file="schema.sql"
    )
    assert tables == []
    # 兩筆都會出現：掃描器自己記一筆「區塊註解未終止」，殘餘的
    # "CREATE TABLE a (x INT" 又因括號不成對而解析失敗，再記一筆——
    # 兩筆並存才符合「絕不靜默丟資料」的要求。
    assert len(unsupported) == 2


def test_parse_failure_reason_includes_more_than_exception_class_name():
    _, unsupported = extract_tables("CREATE TABLE broken (((;", source_file="schema.sql")
    reason = unsupported[0].reason
    assert reason.startswith("CREATE TABLE 解析失敗：")
    detail = reason[len("CREATE TABLE 解析失敗：") :]
    # 純類別名稱（例如 "UnexpectedCharacters"）不含空白；只要 detail 裡出現空白或
    # 冒號，就表示我們保留了例外訊息本身，而不只是類別名。
    assert " " in detail or ":" in detail


def test_unterminated_string_does_not_prevent_later_valid_table():
    """一個未終止的單引號字串只能拖垮到下一個分號為止，不能讓後面整份檔案消失。"""
    text = (
        "CREATE TABLE a (x CHAR(1) DEFAULT 'abc\nCREATE TABLE b (y INT);\nCREATE TABLE c (z INT);\n"
    )
    tables, unsupported = extract_tables(text, source_file="schema.sql")
    assert "c" in [t.name for t in tables]
    assert unsupported  # 損失必須留下痕跡，不能悄悄消失


def test_unterminated_block_comment_does_not_prevent_later_valid_table():
    """一個未終止的區塊註解只能拖垮到下一個分號為止，不能讓後面整份檔案消失。"""
    text = (
        "CREATE TABLE a (x INT /* never closed\nCREATE TABLE b (y INT);\nCREATE TABLE c (z INT);\n"
    )
    tables, unsupported = extract_tables(text, source_file="schema.sql")
    assert "c" in [t.name for t in tables]
    assert unsupported


def test_unterminated_string_is_reported_with_opening_line():
    text = "CREATE TABLE a (x CHAR(1) DEFAULT 'abc\nCREATE TABLE b (y INT);\n"
    _, unsupported = split_statements(text, source_file="schema.sql")
    assert len(unsupported) == 1
    assert "未終止" in unsupported[0].reason
    assert unsupported[0].line == 1


def test_unterminated_block_comment_is_reported_with_opening_line():
    text = "CREATE TABLE a (x INT /* never closed\nCREATE TABLE b (y INT);\n"
    _, unsupported = split_statements(text, source_file="schema.sql")
    assert len(unsupported) == 1
    assert "未終止" in unsupported[0].reason
    assert unsupported[0].line == 1


def test_odd_run_of_quotes_recovers_at_line_end_not_end_of_input():
    """DEFAULT ''' 是奇數個引號：開頭引號 + 逸出的一對，之後仍在字串內，直到行尾
    才恢復掃描；不能因為找不到成對的結尾引號就把整份輸入吃光。"""
    text = (
        "CREATE TABLE a (x CHAR(3) DEFAULT '''\nCREATE TABLE b (y INT);\nCREATE TABLE c (z INT);\n"
    )
    stmts, unsupported = split_statements(text, source_file="schema.sql")
    assert any("未終止" in u.reason for u in unsupported)
    assert unsupported[0].line == 1
    # c 這句仍然被獨立切出來，且行號正確——證明恢復點落在行尾，而非整份輸入結尾。
    assert stmts[-1] == (3, "CREATE TABLE c (z INT)")


def test_unterminated_string_does_not_hang_or_raise():
    """輸入末尾的未終止單引號不能掛起也不能拋例外。"""
    text = "CREATE TABLE a (x CHAR(1) DEFAULT '"
    stmts, unsupported = split_statements(text, source_file="schema.sql")
    assert len(unsupported) == 1
    assert "未終止" in unsupported[0].reason
    assert unsupported[0].line == 1


# --- I3（branch review）：掃描器必須跟單引號一樣認得雙引號字串常值 -------------
#
# `split_statements` 原本只有 `'...'` 這一種字串常值的分支，`--`、`/* */`、`;`
# 都會正確地在單引號字串內被當成資料而非結構符號；但完全沒有 `"..."` 的對應
# 分支。Informix（T100 的資料庫）預設接受 `"..."` 當字串常值，這不是罕見寫法。
# 少了這個分支：字串裡的 `;` 會把語句切斷、字串裡的 `--` 會把同一行剩下的內容
# 當成註解吃掉、字串裡的 `/*` 會觸發一個根本不存在的「區塊註解未終止」假報告。
# 這是單引號那一版缺陷（已修好）的逐字重演，`masking.py` 對 `'` 與 `"` 是對稱
# 處理的，`ddl.py` 原本沒有對稱，是這個分支不一致的落點。


def test_double_quoted_default_containing_semicolon_does_not_split_statement():
    """demonstrated 重現：DEFAULT 值是雙引號字串、內含一個分號，這個分號不能
    被誤判成語句結束符，後面完整的第二個 CREATE TABLE 也必須被擷取到。"""
    text = 'CREATE TABLE a (x CHAR(1) DEFAULT ";");\nCREATE TABLE b (y INT);\n'
    tables, unsupported = extract_tables(text, source_file="schema.sql")
    assert [t.name for t in tables] == ["a", "b"]
    assert unsupported == []


def test_semicolon_inside_double_quoted_string_does_not_split_statement():
    tables, unsupported = extract_tables(
        'CREATE TABLE a (x CHAR(1) DEFAULT ";");', source_file="schema.sql"
    )
    assert len(tables) == 1
    assert unsupported == []


def test_line_comment_marker_inside_double_quoted_string_is_not_a_comment():
    text = 'CREATE TABLE a (x CHAR(20) DEFAULT "--not a comment");\n'
    stmts, unsupported = split_statements(text, source_file="schema.sql")
    assert len(stmts) == 1
    assert "--not a comment" in stmts[0][1]
    assert unsupported == []


def test_unterminated_double_quoted_string_recovers_at_line_end_and_later_table_survives():
    """未終止的雙引號字串只能拖垮到下一個分號為止，行為必須跟單引號的復原規則
    完全對稱：字串未終止時在行尾恢復掃描，受影響範圍只到「下一個分號」為止
    （這裡是 b 那一句的分號，a／b 因此被黏成一句解析失敗的殘餘語句），但再
    後面完整的 CREATE TABLE c 仍要被擷取到，不能因為一個壞掉的字串就讓後面
    整份檔案消失——這與既有的單引號版本 `test_unterminated_string_does_not_
    prevent_later_valid_table` 是同一種形狀。"""
    text = (
        'CREATE TABLE a (x CHAR(1) DEFAULT "abc\nCREATE TABLE b (y INT);\nCREATE TABLE c (z INT);\n'
    )
    tables, unsupported = extract_tables(text, source_file="schema.sql")
    assert "c" in [t.name for t in tables]
    assert any("未終止" in u.reason for u in unsupported)
    assert unsupported[0].line == 1


def test_default_double_quoted_string_is_captured_without_its_quotes():
    tables, unsupported = extract_tables(
        'CREATE TABLE a (x CHAR(10) DEFAULT "abc");', source_file="schema.sql"
    )
    assert unsupported == []
    assert tables[0].columns[0].default == "abc"


def test_crlf_line_endings_are_handled():
    """CRLF 行尾應保留正確行號，未終止字串的爆炸半徑仍受限。"""
    # 第一部分：正常的多行區塊註解（用 CRLF）應保留後續行號
    text_crlf = (
        "CREATE TABLE a (x INT);\r\n/* comment\r\nspans\r\nlines */\r\nCREATE TABLE b (y INT);\r\n"
    )
    stmts, unsupported = split_statements(text_crlf, source_file="schema.sql")
    assert [line for line, _ in stmts] == [1, 5]
    assert unsupported == []

    # 第二部分：CRLF 檔案中的未終止字串，後面的有效表仍能被提取
    text_crlf_unterminated = (
        "CREATE TABLE a (x CHAR(1) DEFAULT 'abc\r\n"
        "CREATE TABLE b (y INT);\r\n"
        "CREATE TABLE c (z INT);\r\n"
    )
    tables, unsupported = extract_tables(text_crlf_unterminated, source_file="schema.sql")
    assert "c" in [t.name for t in tables]
    assert unsupported  # 損失必須留下痕跡
