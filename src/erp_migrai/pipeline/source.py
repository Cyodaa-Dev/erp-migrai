"""來源資料讀取介面：`SourceReader` 與開源版唯一內建的實作 `SqliteSourceReader`。

載入階段（模組五）要從來源資料庫把每一張表的資料讀出來，依 `mapping.json`
轉換後寫進 Odoo。不同 ERP 的來源資料庫種類不同，因此讀取邏輯抽成
`SourceReader` 這個介面：開源版只內建 `SqliteSourceReader`（讀 `make corpus`
產生的 `data.sqlite`，也是本機示範與測試的來源）；其他資料庫種類的讀取器
由商業版另外實作、依這個介面插入——這裡只留介面本身，不夾帶任何與特定
商業資料庫種類相關的字樣。

PK（主鍵）刻意不放進 `mapping.json`，而是由 `primary_key()` 現場向來源
資料庫查——PK 是來源資料本身的性質，讓 `mapping.json` 多背一份副本只會
多一個「跟來源不一致」的地方。`rows()` 依 PK 排序輸出，是為了讓
`migration_log.jsonl` 的行序在每次執行之間保持一致（確定性）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol


class SourceError(Exception):
    """來源資料庫讀不到、表不存在、或結構不符預期。載入階段把它轉成 E-4001。"""


class SourceReader(Protocol):
    """載入階段讀來源資料所需的最小介面。

    Task 7／10／11 依賴這五個方法的確切簽章。開源版只有 `SqliteSourceReader`
    這一種實作。
    """

    def tables(self) -> list[str]:
        """來源資料庫裡的所有表名（不含 `sqlite_% ` 之類的系統表）。"""
        ...

    def primary_key(self, table: str) -> list[str]:
        """`table` 的主鍵欄位，依複合主鍵的序位排序；沒有主鍵則回傳空清單。"""
        ...

    def columns(self, table: str) -> list[str]:
        """`table` 的所有欄位，依宣告順序回傳。"""
        ...

    def rows(self, table: str) -> Iterator[dict[str, Any]]:
        """`table` 的每一列資料，依主鍵排序後逐列產出（惰性，不整批讀進記憶體）。"""
        ...

    def count(self, table: str) -> int:
        """`table` 的總列數。必須是獨立於 `rows()` 的取得路徑（見 `count()` 實作註記）。"""
        ...


class SqliteSourceReader:
    """讀 SQLite 檔案（`make corpus` 產生的 `data.sqlite`）的 `SourceReader` 實作。"""

    def __init__(self, path: Path) -> None:
        # sqlite3.connect() 對不存在的路徑預設會靜默建立一個空的資料庫檔案——
        # 若不在這裡先擋，路徑打錯字只會在後面查表時才報「表不存在」，
        # 而且已經在磁碟上留下一個不該存在的空檔案。開發者/使用者看到的
        # 錯誤應該直接點名「資料庫本身讀不到」，而不是被誤導去查表名。
        if not path.is_file():
            raise SourceError(f"讀不到來源資料庫：{path}")
        self._path = path
        self._con = sqlite3.connect(path)
        self._con.row_factory = sqlite3.Row

    def tables(self) -> list[str]:
        rows = self._con.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        ).fetchall()
        return [row["name"] for row in rows]

    def _check_table(self, table: str) -> None:
        """表名先過白名單再組進 SQL 字串。

        表名來自 `mapping.json`，而 `mapping.json` 可能被人手動編輯過
        （打錯字、複製貼上舊表名……）。`tables()` 的結果直接來自
        `sqlite_master`，是這個資料庫真實存在的表清單，拿它當白名單比對，
        可以在組字串之前就擋下不存在的表名，錯誤訊息也能點名到底是哪個
        表名、哪個資料庫——而不是讓一個從未被檢查過的字串直接流進 SQL，
        變成一個難以追查、訊息又不知所云的底層 sqlite3 例外。
        """
        if table not in self.tables():
            raise SourceError(f"表 {table!r} 不存在於來源資料庫 {self._path}")

    def primary_key(self, table: str) -> list[str]:
        self._check_table(table)
        # PRAGMA table_info 的 pk 欄位：0 表示不是主鍵，非 0 是複合主鍵裡的
        # 1-based 序位——與欄位在 CREATE TABLE 裡的宣告順序無關，必須依這個
        # 數值排序才是真正的主鍵順序。
        info = self._con.execute(f'PRAGMA table_info("{table}")').fetchall()
        pk_columns = sorted((row["pk"], row["name"]) for row in info if row["pk"] > 0)
        return [name for _, name in pk_columns]

    def columns(self, table: str) -> list[str]:
        self._check_table(table)
        info = self._con.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [row["name"] for row in info]

    def rows(self, table: str) -> Iterator[dict[str, Any]]:
        # 白名單檢查放在這個外層的一般函式裡、立即執行——若把它放進下面
        # 那個用 yield 寫的產生器函式本體，Python 只會在第一次疊代時才真正
        # 跑到那一行，呼叫 rows() 當下不會失敗，行為會變得不直覺（表不存在
        # 卻要等到 for 迴圈才報錯）。分成兩層：這一層負責「立即驗證」，
        # `_iter_rows()` 負責「惰性讀列」。
        self._check_table(table)
        return self._iter_rows(table)

    def _iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        # 沒有主鍵的表退而求其次，依全部欄位排序——單一欄位排序在沒有主鍵
        # 保證唯一性的情況下不足以決定順序，但依「全部欄位」排序時，只要
        # 兩列的內容有任何差異，順序就是決定性的；兩列所有欄位完全相同的
        # 情況下彼此順序如何都不影響資料本身。真正的替代方案（例如硬性
        # 要求所有來源表都要有主鍵）不成立：這是真實 T100 會遇到的情況，
        # 載入工具不能因為來源表沒宣告主鍵就直接放棄。
        order_columns = self.primary_key(table) or self.columns(table)
        order_clause = ", ".join(f'"{column}"' for column in order_columns)
        cursor = self._con.execute(f'SELECT * FROM "{table}" ORDER BY {order_clause}')
        for row in cursor:
            yield dict(row)

    def count(self, table: str) -> int:
        # count() 與 rows() 必須是兩條獨立的取得路徑，刻意不透過疊代
        # 逐列讀取再數長度來實作。否則 rows() 若因為某個缺陷少吐一列，
        # Task 11 的 row_count 檢查會分子（實際載入列數）分母（來源列數）
        # 一起錯，兩個數字剛好對得上而顯示全綠——這正是計劃 2 那個
        # Critical 缺陷的形狀。
        self._check_table(table)
        return self._con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
