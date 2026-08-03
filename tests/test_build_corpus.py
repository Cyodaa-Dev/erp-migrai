"""`build_corpus.build_case()` / `main()` 的行為契約。

`data.sqlite` 由兩份可審閱的 SQL（`src/schema.sql` + `data/seed.sql`）生成，
本身不進 git（見 `.gitignore`）。這裡驗證的重點有二：

1. 灌進去的列數必須從資料庫本身數出來，不能從 `seed.sql` 反推——反推會讓
   「seed.sql 寫了 4 列但只灌進 3 列」這種缺陷跟著分母一起消失。
2. 連跑兩次不可以把資料灌成兩倍：`build_case` 必須先刪掉既有的
   `data.sqlite` 再重建，而不是在既有資料庫上疊加執行 `seed.sql`。
"""

from __future__ import annotations

import sqlite3

from erp_migrai.tools.build_corpus import build_case, main


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
    con = sqlite3.connect(db)
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

    con = sqlite3.connect(case / "data" / "data.sqlite")
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_build_case_returns_table_and_row_counts_from_database(tmp_path):
    """回傳值（表數、列數）必須是從資料庫數出來的，不是從輸入檔案推算。"""
    case = tmp_path / "case_probe"
    (case / "src").mkdir(parents=True)
    (case / "data").mkdir()
    (case / "src" / "schema.sql").write_text(
        "CREATE TABLE t1 (a CHAR(4) NOT NULL, PRIMARY KEY (a));\n"
        "CREATE TABLE t2 (b CHAR(4) NOT NULL, PRIMARY KEY (b));\n",
        encoding="utf-8",
    )
    (case / "data" / "seed.sql").write_text(
        "INSERT INTO t1 VALUES ('x');\n"
        "INSERT INTO t1 VALUES ('y');\n"
        "INSERT INTO t2 VALUES ('z');\n",
        encoding="utf-8",
    )

    num_tables, num_rows = build_case(case)

    assert num_tables == 2
    assert num_rows == 3


def test_main_continues_other_cases_and_returns_nonzero_when_one_case_fails(tmp_path, monkeypatch):
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

    assert rc == 1
    # 沒出事的 case 仍然要被處理完，data.sqlite 照樣產生。
    assert (ok_case / "data" / "data.sqlite").is_file()
