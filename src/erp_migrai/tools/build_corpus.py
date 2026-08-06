"""由 `src/schema.sql` 與 `data/seed.sql` 產生各 corpus case 的 `data.sqlite`。

`data.sqlite` 是二進位檔，PR 裡審不了，所以不進 git（見 `.gitignore`）；
真正可審閱、可 commit 的是這兩份 SQL。這個工具的職責就是把它們重新
組裝回一個可查詢的 SQLite 檔案，供 Task 6 之後的驗證階段讀取。

重建而非疊加：每次都先刪掉既有的 `data.sqlite`（如果有的話）再重新執行
`schema.sql` 與 `seed.sql`。若改成在既有資料庫上直接疊加執行 `seed.sql`，
連跑兩次 `make corpus` 就會把資料灌成兩倍——而且這個錯誤要到很後面
（Task 11 的金額比對莫名其妙變成兩倍）才會被發現，那時候很難聯想到
根因在這裡。

## `--dirty`：同一套流程，換一份 seed

`--dirty` 改讀 `data/seed_dirty.sql`、寫出 `data/data_dirty.sqlite`，
其餘（同一份 `src/schema.sql`、先刪再建、失敗刪半成品、從資料庫本身數
表數與列數）完全共用——刻意不另立一套產生器：髒語料若走另一條程式路徑，
「乾淨語料建得出來、髒語料建出來的形狀不一樣」這種差異就沒有任何機制
擋得住，而那正是拿它去示範報錯時最不該出現的變因。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

CORPUS_ROOT = Path(__file__).resolve().parents[3] / "corpus"

# 兩組（seed 檔名, 產出檔名）。抽成常數而不是散在函式簽章的預設值裡，
# 是為了讓 `.gitignore` 要擋的檔名、Makefile 的目標、與這裡的產出對得起來
# 時只有一個地方要看。
CLEAN_SEED = "seed.sql"
CLEAN_DB = "data.sqlite"
DIRTY_SEED = "seed_dirty.sql"
DIRTY_DB = "data_dirty.sqlite"


def build_case(
    case_dir: Path, *, seed_name: str = CLEAN_SEED, db_name: str = CLEAN_DB
) -> tuple[int, int]:
    """建出單一 case 的 `data.sqlite`（或 `--dirty` 的 `data_dirty.sqlite`），
    回傳（表數、總列數）。

    兩個數字都是從建好的資料庫本身 `SELECT COUNT(*)` 數出來，不是從
    `seed.sql` 的行數推算——反推會讓「seed.sql 寫了 4 列但只灌進 3 列」
    這種缺陷跟著分母一起消失。
    """
    schema_sql = (case_dir / "src" / "schema.sql").read_text(encoding="utf-8")
    seed_sql = (case_dir / "data" / seed_name).read_text(encoding="utf-8")

    db_path = case_dir / "data" / db_name
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


def main(argv: Sequence[str] = ()) -> int:
    """`argv` 預設是**空序列**，不是 `None`。

    `argparse.parse_args(None)` 會去讀 `sys.argv[1:]`——那正是 `main()`
    被當成函式呼叫（既有 4 條單元測試就是這樣呼叫的）時最糟的預設值：
    實測加上 argparse 之後，那 4 條測試全部炸在
    `unrecognized arguments: -q`，因為它們吃到的是 **pytest 自己的**
    命令列參數。命令列參數是 `__main__` 那一層的東西，由那裡明確傳進來
    （見檔案最後一行），函式本身不去偷看行程的全域狀態。
    """
    parser = argparse.ArgumentParser(
        prog="python -m erp_migrai.tools.build_corpus",
        description="由 src/schema.sql 與 data/seed.sql 產生各 corpus case 的 data.sqlite",
    )
    parser.add_argument(
        "--dirty",
        action="store_true",
        help=f"改讀 data/{DIRTY_SEED}、寫出 data/{DIRTY_DB}"
        "（示範「誠實報錯」用的髒資料；只處理具備該 seed 的 case）",
    )
    args = parser.parse_args(argv)
    seed_name, db_name = (DIRTY_SEED, DIRTY_DB) if args.dirty else (CLEAN_SEED, CLEAN_DB)

    if not CORPUS_ROOT.is_dir():
        print(f"找不到語料庫目錄：{CORPUS_ROOT}", file=sys.stderr)
        return 1

    cases = sorted(
        p
        for p in CORPUS_ROOT.iterdir()
        if p.is_dir()
        and (p / "src" / "schema.sql").is_file()
        and (p / "data" / seed_name).is_file()
    )
    if not cases:
        # 找不到就是 exit 1，`--dirty` 也一樣不放水：靜默地「沒有 case 要
        # 處理，那就成功結束」會讓 Makefile 目標與 CI 在髒語料被誤刪或改名
        # 之後照樣顯示綠燈，而下一步拿著不存在的 data_dirty.sqlite 去跑
        # load 才發現。
        print(
            f"{CORPUS_ROOT} 底下沒有任何同時具備 src/schema.sql 與 data/{seed_name} 的 case",
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
            num_tables, num_rows = build_case(case, seed_name=seed_name, db_name=db_name)
        except (sqlite3.Error, OSError, UnicodeDecodeError) as exc:
            failed_cases.append((case.name, str(exc)))
            continue

        print(f"✓ {case.name}：{num_tables} 表 / {num_rows} 列 → {case / 'data' / db_name}")

    if failed_cases:
        for case_name, error_msg in failed_cases:
            print(f"✗ {case_name}：{error_msg}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
