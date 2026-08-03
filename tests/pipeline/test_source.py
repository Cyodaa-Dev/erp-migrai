"""`SqliteSourceReader` 的行為契約。

四條核心測試（複合 PK 排序、rows() 依 PK 排序、表不存在丟 SourceError、
count() 不得經由 rows() 計算）之外，另外補了：

- `columns()` 的順序契約（宣告順序，而非 PK 順序）。
- 表沒有主鍵時 `primary_key()`／`rows()` 的行為——這是真實 T100 會遇到的
  情況，不能因為表沒宣告主鍵就整組放棄。
- 表名要先過 `tables()` 白名單才能組進 SQL 字串。
- 資料庫檔案本身就不存在時要儘早報錯，而不是被 sqlite3 靜默建立一個
  空檔案。
- `rows()` 必須是惰性疊代（不整批讀進記憶體），以及各方法對「表不存在」
  的行為要一致，不是只有 `count()` 才檢查。
- `DECIMAL` 欄位從 SQLite 讀回來的型別：只有真的帶非零小數才是 `float`，
  整數值讀回來仍是 `int`——釘住這件事，不要寫成「所有 DECIMAL 都是
  float」這種跟事實不符的斷言。
"""

from __future__ import annotations

import inspect
import sqlite3
import types
from contextlib import closing
from pathlib import Path

import pytest

import erp_migrai.pipeline.source as source_module
from erp_migrai.pipeline.source import SourceError, SqliteSourceReader


def test_primary_key_reads_composite_key_in_order(tmp_path):
    """複合 PK 必須依 PRAGMA 的 pk 序位排序，不是依欄位在表中的宣告順序。

    這兩者在 ord_deta 上剛好一致，所以刻意用一個「宣告順序與 PK 順序不同」
    的表來測——否則這條測試在真正的錯誤下也會通過。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (x CHAR(2), a CHAR(2) NOT NULL, b CHAR(2) NOT NULL,"
            " PRIMARY KEY (b, a));"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    assert reader.primary_key("t") == ["b", "a"]


def test_columns_returns_declaration_order_not_primary_key_order(tmp_path):
    """`columns()` 的順序契約是宣告順序，即使 PK 順序不同也不能被 PK 順序帶偏。

    重用複合 PK 的表：宣告順序是 x, a, b；PK 順序是 b, a。兩者這裡刻意不同，
    才能同時證明 `columns()` 沒有偷偷回傳 PK 順序、也沒有被排序過。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (x CHAR(2), a CHAR(2) NOT NULL, b CHAR(2) NOT NULL,"
            " PRIMARY KEY (b, a));"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    assert reader.columns("t") == ["x", "a", "b"]


def test_rows_are_ordered_by_primary_key(tmp_path):
    """列序必須是確定性的，否則 migration_log.jsonl 每次跑都不一樣。"""
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (a CHAR(2) NOT NULL, b INTEGER, PRIMARY KEY (a));"
            "INSERT INTO t VALUES ('z', 1);"
            "INSERT INTO t VALUES ('a', 2);"
            "INSERT INTO t VALUES ('m', 3);"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    assert [r["a"] for r in reader.rows("t")] == ["a", "m", "z"]


def test_primary_key_returns_empty_list_when_table_has_no_primary_key(tmp_path):
    """沒有宣告 PRIMARY KEY 的表：`primary_key()` 回傳空清單，而不是丟例外。

    這是真實 T100 會遇到的情況（並非所有 DDL 都乾淨地宣告了主鍵），載入
    工具不能因為某張表沒有主鍵就整組放棄，必須有明確、可預期的退讓行為。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript("CREATE TABLE t (b INTEGER, a TEXT);")
        con.commit()

    reader = SqliteSourceReader(db)
    assert reader.primary_key("t") == []


def test_rows_are_deterministic_when_table_has_no_primary_key(tmp_path):
    """沒有主鍵時，`rows()` 退而求其次依「全部欄位」排序，順序仍然是決定性的。

    決定：單一欄位不足以保證唯一、也就不足以保證順序決定性；但依全部欄位
    排序時，只要任兩列的內容有任何差異，順序就是決定性的——兩列所有欄位
    完全相同的情況下彼此順序如何都不影響資料本身，不需要額外處理。
    插入順序刻意打亂（先插 b=2、再 b=1、再 b=3），若退讓排序沒有生效，
    輸出會照插入（rowid）順序，與這裡斷言的 (1, 2, 3) 不同。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (b INTEGER, a TEXT);"
            "INSERT INTO t VALUES (2, 'z');"
            "INSERT INTO t VALUES (1, 'm');"
            "INSERT INTO t VALUES (3, 'a');"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    assert [(r["b"], r["a"]) for r in reader.rows("t")] == [(1, "m"), (2, "z"), (3, "a")]


def test_missing_table_raises_source_error_naming_table_and_path(tmp_path):
    db = tmp_path / "d.sqlite"
    sqlite3.connect(db).close()
    reader = SqliteSourceReader(db)
    with pytest.raises(SourceError) as exc:
        reader.count("no_such_table")
    assert "no_such_table" in str(exc.value)
    assert str(db) in str(exc.value)


def test_missing_table_raises_source_error_for_primary_key_columns_and_rows(tmp_path):
    """「表不存在丟 SourceError」不是只有 `count()` 才要守住。

    Task 6 的 brief 只給了 `count()` 那一條，但 `primary_key()`／`columns()`／
    `rows()` 都會把表名組進 SQL 字串，同樣的白名單檢查必須套用到全部四個
    方法，否則呼叫端（Task 7／10／11）永遠不知道還有哪個方法沒守住。

    `rows()` 是產生器：呼叫當下就要驗證並丟出例外，不能等到疊代時才丟——
    這裡直接呼叫 `reader.rows(...)` 而不先包一層 `list()`／`next()`，
    正是要證明例外在呼叫當下就發生。
    """
    db = tmp_path / "d.sqlite"
    sqlite3.connect(db).close()
    reader = SqliteSourceReader(db)

    with pytest.raises(SourceError):
        reader.primary_key("no_such_table")
    with pytest.raises(SourceError):
        reader.columns("no_such_table")
    with pytest.raises(SourceError):
        reader.rows("no_such_table")


def test_unknown_table_name_is_rejected_before_reaching_sql(tmp_path):
    """表名必須先過 `tables()` 白名單，才能組進 SQL 字串（不能只靠運氣）。

    表名來自 `mapping.json`，可能被人手動編輯過，混進一個帶雙引號／分號的
    怪異字串。若實作沒有先做白名單檢查、直接把表名嵌進 SQL，呼叫端會拿到
    一個難以理解的底層 sqlite3 例外（甚至語法錯誤），而不是點名清楚的
    `SourceError`。這裡驗證兩件事：丟出的是 `SourceError`，而且原本的表 t
    完全沒受影響——證明這個怪字串從未真的抵達 SQLite 執行。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (a CHAR(2) NOT NULL, PRIMARY KEY (a));INSERT INTO t VALUES ('x');"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    with pytest.raises(SourceError):
        reader.count('t"; DROP TABLE t; --')

    with closing(sqlite3.connect(db)) as con:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_missing_database_file_raises_source_error(tmp_path):
    """資料庫檔案本身就不存在時要立刻報錯，而不是被 sqlite3 靜默建立空檔案。

    `sqlite3.connect()` 對不存在的路徑預設會建立一個新的空資料庫——若
    `SqliteSourceReader.__init__` 不擋，路徑打錯字只會在後面查表時才報
    「表不存在」，而且已經在磁碟上留下一個不該出現的空檔案，讓下一次
    除錯的人更困惑。
    """
    missing = tmp_path / "does_not_exist.sqlite"
    with pytest.raises(SourceError) as exc:
        SqliteSourceReader(missing)
    assert str(missing) in str(exc.value)
    assert not missing.exists()


def test_count_does_not_depend_on_rows(tmp_path):
    """count() 必須是獨立的取得路徑。

    若 count() 內部只是 len(list(rows()))，那麼 rows() 少吐一列時，
    Task 11 的 row_count 檢查會分子分母一起錯而顯示全綠——這正是
    計劃 2 那個 Critical 的形狀。

    白箱檢查刻意用 ".rows(" 這個精確字串，不是裸的 "rows"：一個完全正確、
    根本沒呼叫 rows() 的實作，只要寫了 `num_rows = ...` 這種變數名稱，
    裸字串 "rows" 就會誤判成「呼叫了 rows()」而讓這條測試對正確實作也
    報紅。".rows(" 只匹配「呼叫 rows 方法」這個動作本身，`len(list(
    self.rows(table)))` 這種真正錯誤的實作仍然含有 ".rows("，照樣抓得到。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (a CHAR(2) NOT NULL, PRIMARY KEY (a));"
            "INSERT INTO t VALUES ('x');"
            "INSERT INTO t VALUES ('y');"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    source = inspect.getsource(type(reader).count)
    assert ".rows(" not in source, "count() 不得經由 rows() 計算"
    assert reader.count("t") == 2


def test_rows_is_a_lazy_generator_not_a_materialized_list(tmp_path):
    """`rows()` 是 `Iterator`，大表不該一次讀進記憶體。

    這裡用兩層證據：(1) 執行期型別檢查——回傳值必須是真正的產生器物件
    （`types.GeneratorType`），而不是先把整個結果集算成 list 再包成
    iterator；(2) 白箱檢查——原始碼裡不能出現 `.fetchall(`，那是「先把
    整個 cursor 一次吐光」的訊號，跟惰性讀列互斥。

    白箱檢查同樣挑精確字串 `.fetchall(` 而非裸的 "fetchall"，理由與
    `count()` 那條測試的 ".rows(" 一致。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (a CHAR(2) NOT NULL, PRIMARY KEY (a));"
            "INSERT INTO t VALUES ('x');"
            "INSERT INTO t VALUES ('y');"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    result = reader.rows("t")
    assert isinstance(result, types.GeneratorType)
    assert [r["a"] for r in result] == ["x", "y"]

    source = inspect.getsource(type(reader)._iter_rows)
    assert ".fetchall(" not in source, "rows() 不得把整批結果先讀進記憶體"


def test_decimal_column_from_sqlite_is_float_only_when_fractional(tmp_path):
    """`DECIMAL(p,s)` 欄位從 SQLite 讀回來：只有真的帶非零小數才是 `float`。

    已在真的 `corpus/case03_sales_order/data/data.sqlite` 實測過這個形狀：
    `O0001` 第 3 行單價 333.33 讀回來是 `float`，其餘整數金額（1000、250、
    5000）讀回來是 `int`。這裡不依賴那份會被 `.gitignore` 排除、需要先跑
    `make corpus` 才存在的語料檔，改用一個自帶的最小 SQLite 檔重現同一個
    形狀，斷言才不會因為別人還沒跑過 `make corpus` 而莫名其妙失敗。

    刻意不要寫成「所有 DECIMAL 都是 float」——那與事實不符：SQLite 用
    動態型別儲存值本身的字面形式，值恰好是整數時儲存與讀回都是 `int`。
    Task 8 的 `apply_transform` 會把值轉字串；Task 11 的金額比對必須用
    `Decimal(str(value))`，絕對不能寫成 `Decimal(value)`——後者對這裡的
    `float` 值會引入二進位浮點誤差。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE ord_deta (ord_seq INTEGER NOT NULL, ord_price DECIMAL(15,4)"
            " NOT NULL, PRIMARY KEY (ord_seq));"
            "INSERT INTO ord_deta VALUES (1, 1000);"
            "INSERT INTO ord_deta VALUES (2, 250);"
            "INSERT INTO ord_deta VALUES (3, 333.33);"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    prices = {r["ord_seq"]: r["ord_price"] for r in reader.rows("ord_deta")}

    assert prices[1] == 1000
    assert isinstance(prices[1], int)
    assert prices[2] == 250
    assert isinstance(prices[2], int)
    assert prices[3] == 333.33
    assert isinstance(prices[3], float)


def test_source_module_has_no_oracle_references():
    """`source.py` 不得出現任何與商業版讀取器種類相關的字樣（總設計 §2.6）。

    介面留下、臍帶不留：開源版只內建 `SqliteSourceReader`，其他資料庫種類
    的讀取器由商業版另外實作並依 `SourceReader` 介面插入。這條測試是個
    低成本的回歸守衛，避免日後有人在 docstring／註解／import 裡不小心
    夾帶商業版才該提到的具體資料庫種類名稱。
    """
    text = Path(source_module.__file__).read_text(encoding="utf-8").lower()
    assert "oracle" not in text
