"""由 `src/schema.sql` 與 `data/seed.sql` 產生各 corpus case 的 `data.sqlite`。

`data.sqlite` 是二進位檔，PR 裡審不了，所以不進 git（見 `.gitignore`）；
真正可審閱、可 commit 的是這兩份 SQL。這個工具的職責就是把它們重新
組裝回一個可查詢的 SQLite 檔案，供 Task 6 之後的驗證階段讀取。

重建而非疊加：每次都先刪掉既有的 `data.sqlite`（如果有的話）再重新執行
`schema.sql` 與 `seed.sql`。若改成在既有資料庫上直接疊加執行 `seed.sql`，
連跑兩次 `make corpus` 就會把資料灌成兩倍——而且這個錯誤要到很後面
（Task 11 的金額比對莫名其妙變成兩倍）才會被發現，那時候很難聯想到
根因在這裡。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

CORPUS_ROOT = Path(__file__).resolve().parents[3] / "corpus"


def build_case(case_dir: Path) -> tuple[int, int]:
    """建出單一 case 的 `data.sqlite`，回傳（表數、總列數）。

    兩個數字都是從建好的資料庫本身 `SELECT COUNT(*)` 數出來，不是從
    `seed.sql` 的行數推算——反推會讓「seed.sql 寫了 4 列但只灌進 3 列」
    這種缺陷跟著分母一起消失。
    """
    schema_sql = (case_dir / "src" / "schema.sql").read_text(encoding="utf-8")
    seed_sql = (case_dir / "data" / "seed.sql").read_text(encoding="utf-8")

    db_path = case_dir / "data" / "data.sqlite"
    db_path.unlink(missing_ok=True)

    # executescript() 在 autocommit 模式下逐句立即生效：schema.sql／seed.sql
    # 若中途語法錯誤，錯誤前已執行的敘述仍然落地在磁碟上的 data.sqlite——
    # 留下一個 schema 完整、PRAGMA integrity_check 說 ok、只是少了幾列的
    # 半成品。這種半成品比完全沒有輸出更危險：下游（Task 6／11）讀得進去、
    # 算得出金額，只是數字錯的，會被誤判成「遷移少了資料」而不是「語料
    # 根本沒建完」。所以失敗時要把半成品刪掉，寧可讓下游讀不到檔案，也不要
    # 讀到少列的檔案。
    built = False
    con = sqlite3.connect(db_path)
    try:
        con.executescript(schema_sql)
        con.executescript(seed_sql)

        tables = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        num_rows = sum(
            con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables
        )
        built = True
    finally:
        con.close()
        if not built:
            db_path.unlink(missing_ok=True)

    return len(tables), num_rows


def main() -> int:
    if not CORPUS_ROOT.is_dir():
        print(f"找不到語料庫目錄：{CORPUS_ROOT}", file=sys.stderr)
        return 1

    cases = sorted(
        p
        for p in CORPUS_ROOT.iterdir()
        if p.is_dir()
        and (p / "src" / "schema.sql").is_file()
        and (p / "data" / "seed.sql").is_file()
    )
    if not cases:
        print(
            f"{CORPUS_ROOT} 底下沒有任何同時具備 src/schema.sql 與 data/seed.sql 的 case",
            file=sys.stderr,
        )
        return 1

    # 一個 case 的 SQL 有錯不能讓其餘 case 陪葬（precedent：
    # update_golden.main()），但整體 exit code 必須非 0。
    #
    # 攔截範圍不能只有 sqlite3.Error：data/ 目錄唯讀、schema.sql 不是 UTF-8、
    # data.sqlite 這個路徑被佔用成目錄……這些都會讓某一個 case 噴出
    # PermissionError／UnicodeDecodeError／OSError，若沒攔到就會讓 main()
    # 直接中斷、後面排序在後的好 case 完全沒被處理到。
    failed_cases: list[tuple[str, str]] = []

    for case in cases:
        try:
            num_tables, num_rows = build_case(case)
        except (sqlite3.Error, OSError, UnicodeDecodeError) as exc:
            failed_cases.append((case.name, str(exc)))
            continue

        print(f"✓ {case.name}：{num_tables} 表 / {num_rows} 列 → {case / 'data' / 'data.sqlite'}")

    if failed_cases:
        for case_name, error_msg in failed_cases:
            print(f"✗ {case_name}：{error_msg}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
