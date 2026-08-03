"""`SqliteSourceReader` 的行為契約。

四條核心測試（複合 PK 排序、rows() 依 PK 排序、表不存在丟 SourceError、
count() 不得經由 rows() 計算）之外，另外補了：

- `columns()` 的順序契約（宣告順序，而非 PK 順序）。
- 表沒有主鍵時 `primary_key()`／`rows()` 的行為——這是真實 T100 會遇到的
  情況，不能因為表沒宣告主鍵就整組放棄。
- 表名要先過 `tables()` 白名單才能組進 SQL 字串，且表名／欄位名本身含
  雙引號時要能正確跳脫。
- 資料庫檔案本身就不存在、或存在但內容根本不是 SQLite 資料庫時，都要
  儘早報錯，而不是被 sqlite3 靜默建立空檔案或延後到第一次查詢才炸。
- `rows()` 必須是惰性疊代（不整批讀進記憶體），以及各方法對「表不存在」
  的行為要一致，不是只有 `count()` 才檢查。
- `DECIMAL` 欄位從 SQLite 讀回來的型別：只有真的帶非零小數才是 `float`，
  整數值讀回來仍是 `int`——釘住這件事，不要寫成「所有 DECIMAL 都是
  float」這種跟事實不符的斷言。
- `count()`／`rows()` 的兩條核心守衛（複審後）改成行為式證明，不再是
  字串比對——原因見各測試的 docstring。
- `tables()` 的三個行為（排除系統表、排除 view、依名稱排序）。
- `SourceReader` 這個 Protocol 是否真的被 `SqliteSourceReader` 滿足
  （方法存在 + 簽章一致）。
- `close()`／context manager 的連線生命週期。
- 表名大小寫不符時，錯誤訊息要點名「大小寫不符」而不是誤導成打錯字。

## 為什麼 count()／rows() 的關鍵守衛從字串比對改成行為式證明

原本的實作用 `inspect.getsource` 找 `".rows("` / `".fetchall("` 這種精確
字面樣式，比裸字串 `"rows"` / `"fetchall"` 安全一些，但複審時發現三種
「行為上真的違反契約」的等價改法都能繞過字串比對：

- `count()` 改成 `sum(1 for _ in self._iter_rows(table))`——`_iter_rows` 是
  這個實作自己新增的私有方法，字面上不含 `.rows(`。
- `count()` 改成 `getattr(self, "ro" + "ws")(table)`——動態組出方法名，
  字面上同樣不含 `.rows(`。
- `rows()` 改成 `list(self._con.execute(...))` 或 `cur.fetchmany(10**9)`——
  兩者都是「先整批取出、再逐個 yield」，回傳值仍是 `types.GeneratorType`，
  字面上也不含 `.fetchall(`。

字串比對守的是「原始碼長什麼樣子」，不是「執行時真的做了什麼」。下面
兩條測試改成側錄真正送進 SQLite 的 SQL、或用會在特定時機爆炸的資料
逼出「有沒有提早整批讀取」的證據，不管實作內部呼叫哪個方法名稱都測得到。
"""

from __future__ import annotations

import inspect
import sqlite3
import types
from contextlib import closing
from pathlib import Path

import pytest

import erp_migrai.pipeline.source as source_module
from erp_migrai.pipeline.source import SourceError, SourceReader, SqliteSourceReader


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


def test_rows_are_ordered_by_full_composite_key_position_not_column_name(tmp_path):
    """複合 PK 上，`rows()` 必須依 PK 的序位排序，不是依欄位名字典序。

    複審發現：把實作裡的 `order_columns = self.primary_key(table)` 改成
    `sorted(self.primary_key(table))`（依欄位名字典序而非 pk 序位），原本
    13 條測試全部通過——因為唯一有複合 PK 的 fixture（`test_columns_returns_
    declaration_order_not_primary_key_order` 用的那張表）從來沒插過資料，
    其他排序測試用的都是單一欄位 PK，字典序與 pk 序位在單一欄位上根本沒有
    分別。這裡用 PK 為 `(b, a)` 的表插兩筆資料，PK 序位排序（依 b 再依 a）
    與欄位名字典序排序（依 a 再依 b）對這兩筆資料會得到相反的順序，兩種
    實作在這條測試上才分辨得出來。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (x CHAR(2), a CHAR(2) NOT NULL, b CHAR(2) NOT NULL,"
            " PRIMARY KEY (b, a));"
            "INSERT INTO t VALUES ('1', 'p', 'q');"
            "INSERT INTO t VALUES ('2', 'q', 'p');"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    # 依 (b, a) 排序：第二列 b='p' 排在第一列 b='q' 之前 → ['2', '1']。
    # 若誤依欄位名字典序排成 (a, b)，會得到 ['1', '2']（依 a='p' 排在
    # a='q' 之前）——兩者刻意相反，才能當場分辨誰是誰。
    assert [r["x"] for r in reader.rows("t")] == ["2", "1"]


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


def test_table_name_with_double_quote_is_escaped(tmp_path):
    """表名／欄位名本身合法地含雙引號時，組進 SQL 字串要跳脫，不能直接炸。

    白名單檢查（`_check_table`）只判斷「這個名字存在於 `tables()`」，不
    判斷這個名字好不好組進 SQL 字串。SQLite 允許表名含雙引號（用兩個
    雙引號跳脫宣告），如果組字串時不跳脫，即使表名通過了白名單，組字串
    那一步仍然會炸出 `sqlite3.OperationalError: unrecognized token`——
    又是一次底層例外洩漏給呼叫端，而不是任務書要求的 `SourceError`。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            'CREATE TABLE "we""ird" (a CHAR(2) NOT NULL, PRIMARY KEY (a));'
            'INSERT INTO "we""ird" VALUES (\'x\');'
        )
        con.commit()

    reader = SqliteSourceReader(db)
    assert reader.count('we"ird') == 1
    assert [r["a"] for r in reader.rows('we"ird')] == ["x"]


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


def test_non_sqlite_file_raises_source_error_naming_path(tmp_path):
    """檔案存在但內容不是 SQLite 資料庫時，也要在 `__init__` 當下報 `SourceError`。

    `sqlite3.connect()` 對「檔案存在但內容不是資料庫」的檔案一樣會惰性地
    成功回傳連線物件——`is_file()` 檢查擋不住這種情況，錯誤要等到第一次
    真正查詢時才會冒出來，且冒出來的是使用者看不懂的
    `sqlite3.DatabaseError: file is not a database`。使用者指到半下載的
    檔案、被改副檔名的 CSV、Git LFS pointer 都會觸發這個情況——
    `__init__` 必須提早探測，把錯誤轉成點名路徑的 `SourceError`，這樣
    Task 10／11 才能把它轉成 `E-4001`，而不是讓 sqlite3 的例外類別直接
    洩漏給使用者。
    """
    bogus = tmp_path / "not_a_db.sqlite"
    bogus.write_text("this is definitely not a sqlite file, just plain text\n" * 10)

    with pytest.raises(SourceError) as exc:
        SqliteSourceReader(bogus)
    assert str(bogus) in str(exc.value)


def test_missing_table_error_hints_case_mismatch(tmp_path):
    """表名大小寫不符時，錯誤訊息要點名大小寫問題，而不是誤導成「打錯字」。

    SQLite 本身比對表名不分大小寫，但 `_check_table` 的白名單比對是分
    大小寫的字串相等——`tables()` 回傳的是資料庫裡宣告的實際大小寫。若
    庫裡是 `Ord_Deta`、`mapping.json` 寫成 `ord_deta`，使用者看到單純的
    「表不存在」會先去檢查表名有沒有打錯字，而看不出真正原因是大小寫
    不符。這裡只驗證錯誤訊息有沒有點名真正的表名，行為（仍然丟
    `SourceError`）不變。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript("CREATE TABLE Ord_Deta (a TEXT);")
        con.commit()

    reader = SqliteSourceReader(db)
    with pytest.raises(SourceError) as exc:
        reader.count("ord_deta")
    assert "Ord_Deta" in str(exc.value)


def test_count_issues_a_count_query_and_never_scans_rows(tmp_path):
    """`count()` 必須真的送出一條 COUNT 查詢，不能換一條路徑偷渡回 rows()。

    複審發現：白箱字串比對 `".rows(" not in source` 擋得住的只有「原始碼
    字面上寫了 `.rows(`」這一種寫法。以下三種等價改法都是「count() 依賴
    rows() 的取得路徑」這個真正要禁止的行為，但字面上都不含 `.rows(`，
    字串守衛完全看不見：

    - `sum(1 for _ in self._iter_rows(table))`——`_iter_rows` 是這個實作
      自己新增的私有方法，日後任何人想「重用現成的讀列邏輯」最自然的
      寫法就是它。
    - `getattr(self, "ro" + "ws")(table)`——動態組出方法名再呼叫。
    - `len(list(self . rows (table)))`——只是多打了幾個空白，字面樣式
      `".rows("` 就不再精確匹配。

    改成側錄真正送進 SQLite 底層連線的 SQL 語句：只要 `count()` 真的是
    獨立的取得路徑，側錄到的 SQL 裡就會有一條含 `COUNT(`，且不會有任何
    一條是 `SELECT *` 開頭的全表掃描；不管實作內部呼叫哪個方法名稱做到
    這件事，這個行為式的判斷標準都測得到。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (a CHAR(2) NOT NULL, PRIMARY KEY (a));"
            "INSERT INTO t VALUES ('x');"
            "INSERT INTO t VALUES ('y');"
            "INSERT INTO t VALUES ('z');"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    seen: list[str] = []
    real_execute = type(reader._con).execute

    class _ExecuteSpy:
        """側錄每一條真正送進 SQLite 的 SQL，讓黑箱測試看得見底層取得路徑。"""

        def __init__(self, con: sqlite3.Connection) -> None:
            self._con = con

        def __getattr__(self, name: str):
            return getattr(self._con, name)

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            seen.append(sql)
            return real_execute(self._con, sql, *args)

    reader._con = _ExecuteSpy(reader._con)

    assert reader.count("t") == 3
    assert any("COUNT(" in sql for sql in seen), seen
    assert not any(sql.lstrip().upper().startswith("SELECT *") for sql in seen), seen


def test_rows_is_lazy_and_yields_before_reading_the_whole_result_set(tmp_path):
    """`rows()` 是惰性疊代，不能在回傳第一列之前就把整個結果集讀進記憶體。

    複審發現：白箱字串比對 `".fetchall(" not in source` 擋得住的只有原始碼
    字面上寫了 `.fetchall(` 這一種寫法。`cursor = list(self._con.execute(
    ...))` 或 `cur = ...; cursor = cur.fetchmany(10**9)` 這兩種等價的
    「先整批取出、再逐個 yield」寫法，回傳值仍然是 `types.GeneratorType`
    （外層仍然用了 `yield`），`isinstance` 檢查一樣看不出來；字面上也不含
    `.fetchall(`，字串守衛同樣穿透。

    改成行為式證明：借用 sqlite3 的 `text_factory`，讓最後一列的資料在
    從底層轉換成 Python 值時故意爆炸。真正逐列惰性讀取的實作，前面幾列
    必須先吐得出來，只有真的讀到最後一列時才會炸；任何「先把整個結果集
    materialize 起來、再逐個 yield」的實作，materialize 那一步本身就會
    踩到最後一列，導致連第一次 `next()` 都還沒吐值就先爆炸。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE t (a CHAR(2) NOT NULL, PRIMARY KEY (a));"
            "INSERT INTO t VALUES ('x');"
            "INSERT INTO t VALUES ('y');"
            "INSERT INTO t VALUES ('z');"
        )
        con.commit()

    reader = SqliteSourceReader(db)

    result = reader.rows("t")
    assert isinstance(result, types.GeneratorType)

    def boom(raw: bytes) -> str:
        value = raw.decode()
        if value == "z":
            raise RuntimeError("最後一列爆炸——只有整批 materialize 的實作才會在這裡就死")
        return value

    reader._con.text_factory = boom

    it = reader.rows("t")
    assert next(it)["a"] == "x"
    assert next(it)["a"] == "y"
    with pytest.raises(RuntimeError):
        next(it)


def test_rows_yield_plain_dicts_not_sqlite_row_objects(tmp_path):
    """`rows()` 必須吐純 `dict`，不能是底層的 `sqlite3.Row`。

    `sqlite3.Row` 不支援 `.get()`、`in`、`json.dumps()`——Task 10 要把每一列
    寫進 `migration_log.jsonl` 時就會直接炸掉。既有測試都只用 `r["a"]`
    這種取值方式，`sqlite3.Row` 與 `dict` 在這個用法上分辨不出來，所以
    另外用型別與整體相等性直接斷言。
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
    result = list(reader.rows("t"))
    assert result == [{"a": "x"}, {"a": "y"}]
    assert all(type(row) is dict for row in result)


def test_tables_excludes_system_tables_and_views_and_sorts_by_name(tmp_path):
    """`tables()` 的三個行為：排除 `sqlite_%` 系統表、排除 view、依名稱排序。

    複審發現這三個行為完全沒有測試直接呼叫 `tables()` 覆蓋到：拿掉
    `AND name NOT LIKE 'sqlite_%'`、拿掉 `ORDER BY name`、把 view 也當表
    回傳，三個 mutation 全部存活。`tables()` 是 Protocol 五個方法之一，
    Task 7 的 `plan_load` 會用；排序不穩會直接影響下游輸出順序（違反
    確定性），系統表混進來會讓 `sqlite_sequence` 被誤當成一張來源表。

    這裡建一張 `AUTOINCREMENT` 表以產生 `sqlite_sequence`、建一個 view，
    而且刻意讓建表順序（z, a, m）與字典序相反，才能同時證明「排除系統表」
    「排除 view」「依名稱排序」三件事，而不是巧合通過。
    """
    db = tmp_path / "d.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(
            "CREATE TABLE z (id INTEGER PRIMARY KEY AUTOINCREMENT);"
            "CREATE TABLE a (id INTEGER PRIMARY KEY AUTOINCREMENT);"
            "CREATE TABLE m (id INTEGER PRIMARY KEY AUTOINCREMENT);"
            "INSERT INTO z DEFAULT VALUES;"
            "CREATE VIEW v AS SELECT * FROM z;"
        )
        con.commit()

    reader = SqliteSourceReader(db)
    assert reader.tables() == ["a", "m", "z"]


def test_sqlite_source_reader_satisfies_source_reader_protocol(tmp_path):
    """`SqliteSourceReader` 必須通得過 `SourceReader` 的執行期檢查。

    `pyproject.toml` 的 dev 依賴只有 `pytest` 與 `ruff`，沒有 mypy／
    pyright，`Protocol` 的簽章不會被任何靜態工具檢查。若沒有
    `@runtime_checkable`，`isinstance` 連「方法名都存在」這麼粗淺的檢查
    都做不到（直接丟 `TypeError`）；標了之後，Protocol 多宣告一個
    `SqliteSourceReader` 沒有實作的方法，這條測試就會先炸。
    """
    db = tmp_path / "d.sqlite"
    sqlite3.connect(db).close()
    reader = SqliteSourceReader(db)
    assert isinstance(reader, SourceReader)


def test_sqlite_source_reader_signatures_match_protocol():
    """`isinstance` 只驗方法名存在，不驗簽章——參數改名一樣能通過 isinstance。

    Task 7／10／11 是照 `SourceReader` 這個介面的「確切簽章」（含參數名稱
    與順序）呼叫方法，光靠 `@runtime_checkable` 的 `isinstance` 擋不住
    `SqliteSourceReader.rows` 把參數 `table` 偷偷改名成 `table_name`這種
    介面漂移——呼叫端如果用關鍵字引數呼叫，只會在執行期才炸，而不是在
    這裡就被抓到。這裡直接比對 `inspect.signature` 的參數名稱與順序，
    兩邊漂移就會在這條測試上先炸開，不必等到 Task 7 才發現。
    """
    protocol = SourceReader
    impl = SqliteSourceReader
    for name in ("tables", "primary_key", "columns", "rows", "count"):
        protocol_params = list(inspect.signature(getattr(protocol, name)).parameters)
        impl_params = list(inspect.signature(getattr(impl, name)).parameters)
        assert protocol_params == impl_params, name


def test_close_closes_the_underlying_connection(tmp_path):
    """`close()` 要真的關閉底層連線。

    Task 10／11 會為每個來源開一個 reader，沒有 `close()` 就是連線一路開
    到行程結束。這裡只驗證「呼叫過 `close()` 之後，底層連線已經關閉」——
    用底層連線關閉後執行查詢會丟 `sqlite3.ProgrammingError` 這個標準行為
    來證明，不驗證 reader 自身要多做什麼防呆（例如重複呼叫 `close()` 要
    不要報錯），那超出目前用得到的範圍。
    """
    db = tmp_path / "d.sqlite"
    sqlite3.connect(db).close()
    reader = SqliteSourceReader(db)
    reader.close()
    with pytest.raises(sqlite3.ProgrammingError):
        reader._con.execute("SELECT 1")


def test_context_manager_closes_connection_on_exit(tmp_path):
    """`with SqliteSourceReader(...) as reader:` 離開區塊後連線要關閉。"""
    db = tmp_path / "d.sqlite"
    sqlite3.connect(db).close()
    with SqliteSourceReader(db) as reader:
        assert reader.tables() == []
    with pytest.raises(sqlite3.ProgrammingError):
        reader._con.execute("SELECT 1")


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
