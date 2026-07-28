"""內嵌 SQL 的種類與表名擷取（啟發式）。"""

from erp_migrai.inventory.sqlref import extract_sql


def test_select_from_single_table():
    stmt = extract_sql("SELECT COUNT(*) INTO l_cnt FROM cust_mast", 5)
    assert stmt is not None
    assert stmt.kind == "SELECT"
    assert stmt.tables == ["cust_mast"]
    assert stmt.line == 5


def test_select_from_multiple_tables():
    stmt = extract_sql("SELECT a.x, b.y FROM cust_mast a, cust_addr b WHERE a.k = b.k", 1)
    assert stmt.kind == "SELECT"
    assert stmt.tables == ["cust_addr", "cust_mast"]


def test_insert_into():
    stmt = extract_sql("INSERT INTO cust_mast VALUES (p_cust.*)", 2)
    assert stmt.kind == "INSERT"
    assert stmt.tables == ["cust_mast"]


def test_update():
    stmt = extract_sql("UPDATE cust_mast SET cust_name = p_name WHERE cust_no = p_no", 3)
    assert stmt.kind == "UPDATE"
    assert stmt.tables == ["cust_mast"]


def test_delete_from():
    stmt = extract_sql("DELETE FROM cust_mast WHERE cust_no = p_no", 4)
    assert stmt.kind == "DELETE"
    assert stmt.tables == ["cust_mast"]


def test_declare_cursor_reports_the_inner_select_tables():
    stmt = extract_sql("DECLARE c1 CURSOR FOR SELECT * FROM cust_mast", 7)
    assert stmt.kind == "DECLARE_CURSOR"
    assert stmt.tables == ["cust_mast"]


def test_non_sql_statement_returns_none():
    assert extract_sql("CALL cust_maintain()", 1) is None


def test_tables_are_sorted_and_deduplicated():
    stmt = extract_sql("SELECT * FROM b, a, b", 1)
    assert stmt.tables == ["a", "b"]
