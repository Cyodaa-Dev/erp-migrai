"""`build_corpus.build_case()` / `main()` 的行為契約。

`data.sqlite` 由兩份可審閱的 SQL（`src/schema.sql` + `data/seed.sql`）生成，
本身不進 git（見 `.gitignore`）。這裡驗證的重點有四：

1. 灌進去的列數必須從資料庫本身數出來，不能從 `seed.sql` 反推——反推會讓
   「seed.sql 寫了 4 列但只灌進 3 列」這種缺陷跟著分母一起消失。
2. 連跑兩次不可以把資料灌成兩倍：`build_case` 必須先刪掉既有的
   `data.sqlite` 再重建，而不是在既有資料庫上疊加執行 `seed.sql`。
3. `schema.sql`／`seed.sql` 中途語法錯誤時，不得留下少了幾列、卻「看起來
   完全正常」的半成品 `data.sqlite`——那比完全沒有輸出更危險。
4. 一個 case 失敗（不論是 `sqlite3.Error` 還是 `data/` 目錄唯讀這類
   `OSError`）不能讓其餘 case 陪葬，但整體 exit code 必須非 0。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing

import pytest

from erp_migrai.tools.build_corpus import build_case, main

# chmod 500 在 root 下沒有意義（root 會略過檔案權限檢查），這類測試在以 root
# 執行的環境（例如某些容器）下必須乾淨地跳過，而不是用假陽性通過或報錯。
# precedent：tests/test_cli.py 的 _IS_ROOT。
_IS_ROOT = hasattr(os, "getuid") and os.getuid() == 0


def test_build_corpus_creates_sqlite_with_expected_row_counts(tmp_path):
    """建出來的 data.sqlite 必須含 seed.sql 的每一列。

    列數斷言刻意寫死預期值而非從 seed.sql 反推——反推會讓「seed.sql 有 4 列
    但只灌進 3 列」這種缺陷跟著分母一起消失。
    """
    case = tmp_path / "case_probe"
    (case / "src").mkdir(parents=True)
    (case / "data").mkdir()
    (case / "src" / "schema.sql").write_text(
        "CREATE TABLE t (a CHAR(4) NOT NULL, b INTEGER, PRIMARY KEY (a));\n",
        encoding="utf-8",
    )
    (case / "data" / "seed.sql").write_text(
        "INSERT INTO t VALUES ('x', 1);\nINSERT INTO t VALUES ('y', 2);\n",
        encoding="utf-8",
    )

    build_case(case)

    db = case / "data" / "data.sqlite"
    assert db.is_file()
    with closing(sqlite3.connect(db)) as con:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_build_corpus_rebuilds_instead_of_appending(tmp_path):
    """連跑兩次不可以把資料灌成兩倍。

    這是本工具最容易寫錯的一件事，而且錯了之後 Task 11 的金額比對會
    莫名其妙變成兩倍——那時候很難聯想到根因在這裡。
    """
    case = tmp_path / "case_probe"
    (case / "src").mkdir(parents=True)
    (case / "data").mkdir()
    (case / "src" / "schema.sql").write_text(
        "CREATE TABLE t (a CHAR(4) NOT NULL, PRIMARY KEY (a));\n", encoding="utf-8"
    )
    (case / "data" / "seed.sql").write_text("INSERT INTO t VALUES ('x');\n", encoding="utf-8")

    build_case(case)
    build_case(case)

    with closing(sqlite3.connect(case / "data" / "data.sqlite")) as con:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_build_case_returns_table_and_row_counts_from_database(tmp_path):
    """回傳值（表數、列數）必須是從資料庫數出來的，不是從輸入檔案推算。

    schema.sql／seed.sql 的「字面行數」刻意跟真實表數／列數脫鉤，這樣才能
    分辨「真的查資料庫」與「反推字面行數」這兩種實作：

    - `t1`、`t2` 的 `CREATE TABLE` 擠在同一行——字面只找得到 1 行以
      「CREATE TABLE」開頭，實際是 2 張表；
    - `t3` 的 `CREATE TABLE` 關鍵字本身跨行拆開（`CREATE` 和 `TABLE t3 (`
      分屬兩行）——字面完全數不到任何一行以「CREATE TABLE」開頭，實際仍是
      第 3 張表；
    - seed 用一行多值 `INSERT` 灌 3 列——字面只找得到 1 行以「INSERT」
      開頭，實際是 3 列；`t2`／`t3` 完全沒有資料。

    若把 `build_case()` 的計數換成「數 schema_sql／seed_sql 裡以 CREATE
    TABLE／INSERT 開頭的行數」，這種反推版本在這份 fixture 上會分別算出
    1 表、1 列，跟這裡斷言的 3 表、3 列對不上——測試會變紅。
    """
    case = tmp_path / "case_probe"
    (case / "src").mkdir(parents=True)
    (case / "data").mkdir()
    (case / "src" / "schema.sql").write_text(
        "CREATE TABLE t1 (a CHAR(4) NOT NULL, PRIMARY KEY (a)); "
        "CREATE TABLE t2 (b CHAR(4) NOT NULL, PRIMARY KEY (b));\n"
        "CREATE\n"
        "    TABLE t3 (c CHAR(4) NOT NULL, PRIMARY KEY (c));\n",
        encoding="utf-8",
    )
    (case / "data" / "seed.sql").write_text(
        "INSERT INTO t1 VALUES ('x'), ('y'), ('z');\n",
        encoding="utf-8",
    )

    num_tables, num_rows = build_case(case)

    assert num_tables == 3
    assert num_rows == 3


def test_build_case_does_not_leave_half_built_sqlite_on_failure(tmp_path):
    """schema.sql／seed.sql 中途語法錯誤時，不得留下半成品 data.sqlite。

    `executescript()` 在 autocommit 下逐句立即生效：錯誤前已執行的 INSERT
    仍會落地在磁碟上。留下這種「schema 完整、只是少了幾列」的半成品比完全
    沒有輸出更危險——下游會誤把「語料根本沒建完」當成「遷移少了資料」。

    這裡只斷言「失敗後 data.sqlite 不存在」，不綁死任何實作機制（例如不
    要求一定要用 temp+rename 的原子寫入）。
    """
    case = tmp_path / "case_probe"
    (case / "src").mkdir(parents=True)
    (case / "data").mkdir()
    (case / "src" / "schema.sql").write_text(
        "CREATE TABLE t (a CHAR(4) NOT NULL, PRIMARY KEY (a));\n", encoding="utf-8"
    )
    (case / "data" / "seed.sql").write_text(
        "INSERT INTO t VALUES ('x');\nINSERT INTO t VALUES ('y');\nTHIS IS NOT VALID SQL;\n",
        encoding="utf-8",
    )

    with pytest.raises(sqlite3.Error):
        build_case(case)

    assert not (case / "data" / "data.sqlite").exists()


def test_main_continues_other_cases_and_returns_nonzero_when_one_case_fails(
    tmp_path, monkeypatch, capsys
):
    """一個 case 的 SQL 有錯不能讓其餘 case 陪葬，但整體 exit code 必須非 0。"""
    corpus_root = tmp_path / "corpus"

    ok_case = corpus_root / "case_ok"
    (ok_case / "src").mkdir(parents=True)
    (ok_case / "data").mkdir()
    (ok_case / "src" / "schema.sql").write_text(
        "CREATE TABLE t (a CHAR(4) NOT NULL, PRIMARY KEY (a));\n", encoding="utf-8"
    )
    (ok_case / "data" / "seed.sql").write_text("INSERT INTO t VALUES ('x');\n", encoding="utf-8")

    broken_case = corpus_root / "case_broken"
    (broken_case / "src").mkdir(parents=True)
    (broken_case / "data").mkdir()
    (broken_case / "src" / "schema.sql").write_text("THIS IS NOT VALID SQL;\n", encoding="utf-8")
    (broken_case / "data" / "seed.sql").write_text(
        "INSERT INTO t VALUES ('x');\n", encoding="utf-8"
    )

    import erp_migrai.tools.build_corpus as build_corpus_module

    monkeypatch.setattr(build_corpus_module, "CORPUS_ROOT", corpus_root)

    rc = main()
    captured = capsys.readouterr()

    assert rc == 1
    # 沒出事的 case 仍然要被處理完，data.sqlite 照樣產生，且內容（列數）正確。
    ok_db = ok_case / "data" / "data.sqlite"
    assert ok_db.is_file()
    with closing(sqlite3.connect(ok_db)) as con:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    # 壞掉的 case 要被點名在 stderr，不能默默吞掉。
    assert "case_broken" in captured.err


@pytest.mark.skipif(_IS_ROOT, reason="root 會略過檔案權限檢查，chmod 500 測試在 root 下沒有意義")
def test_main_continues_other_cases_when_one_case_hits_permission_error(tmp_path, monkeypatch):
    """一個 case 失敗的原因不限於 sqlite3.Error：data/ 目錄唯讀時仍不能讓其餘
    case 陪葬（實測：`PermissionError` 若沒被攔到，`main()` 會直接中斷，排序
    在後的好 case 完全沒被處理到）。

    重現方式：先在 broken_case 留一份既有的 data.sqlite（模擬上一次
    `make corpus` 的產物），再把 data/ 目錄設為唯讀。`build_case()` 開頭的
    `db_path.unlink()` 需要對目錄的寫入權限才能刪除既有檔案，因而觸發
    `PermissionError`（若檔案還不存在，`unlink(missing_ok=True)` 在唯讀
    目錄下不會出錯，重現不出這個缺陷）。
    """
    corpus_root = tmp_path / "corpus"

    # 刻意讓壞 case 的目錄名排在好 case 之前，這樣才能驗證「後面的 case
    # 仍被處理到」而不是「剛好還沒輪到就結束了」。
    broken_case = corpus_root / "case_a_broken"
    (broken_case / "src").mkdir(parents=True)
    (broken_case / "data").mkdir()
    (broken_case / "src" / "schema.sql").write_text(
        "CREATE TABLE t (a CHAR(4) NOT NULL, PRIMARY KEY (a));\n", encoding="utf-8"
    )
    (broken_case / "data" / "seed.sql").write_text(
        "INSERT INTO t VALUES ('x');\n", encoding="utf-8"
    )
    (broken_case / "data" / "data.sqlite").touch()

    ok_case = corpus_root / "case_z_ok"
    (ok_case / "src").mkdir(parents=True)
    (ok_case / "data").mkdir()
    (ok_case / "src" / "schema.sql").write_text(
        "CREATE TABLE t (a CHAR(4) NOT NULL, PRIMARY KEY (a));\n", encoding="utf-8"
    )
    (ok_case / "data" / "seed.sql").write_text("INSERT INTO t VALUES ('x');\n", encoding="utf-8")

    import erp_migrai.tools.build_corpus as build_corpus_module

    monkeypatch.setattr(build_corpus_module, "CORPUS_ROOT", corpus_root)

    (broken_case / "data").chmod(0o500)
    try:
        rc = main()
    finally:
        # 必須恢復權限以便 pytest 清理 tmp_path。
        (broken_case / "data").chmod(0o755)

    assert rc == 1
    assert (ok_case / "data" / "data.sqlite").is_file()


def test_main_returns_nonzero_when_corpus_dir_missing(tmp_path, monkeypatch):
    """`CORPUS_ROOT` 指到不存在的路徑時，不能默默回傳 0。"""
    import erp_migrai.tools.build_corpus as build_corpus_module

    monkeypatch.setattr(build_corpus_module, "CORPUS_ROOT", tmp_path / "does_not_exist")

    assert main() == 1


def test_main_returns_nonzero_when_corpus_dir_has_no_cases(tmp_path, monkeypatch):
    """`corpus/` 目錄存在，但沒有任何一個 case 同時具備 `src/schema.sql` 與
    `data/seed.sql` 時，不能默默回傳 0。"""
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()

    import erp_migrai.tools.build_corpus as build_corpus_module

    monkeypatch.setattr(build_corpus_module, "CORPUS_ROOT", corpus_root)

    assert main() == 1
