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

    con = sqlite3.connect(db_path)
    try:
        con.executescript(schema_sql)
        con.executescript(seed_sql)

        tables = [
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        ]
        num_rows = sum(
            con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
        )
    finally:
        con.close()

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
    failed_cases: list[tuple[str, str]] = []
    succeeded = 0

    for case in cases:
        try:
            num_tables, num_rows = build_case(case)
        except sqlite3.Error as exc:
            failed_cases.append((case.name, str(exc)))
            continue

        succeeded += 1
        print(f"✓ {case.name}：{num_tables} 表 / {num_rows} 列 → {case / 'data' / 'data.sqlite'}")

    if failed_cases:
        for case_name, error_msg in failed_cases:
            print(f"✗ {case_name}：{error_msg}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
