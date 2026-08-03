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
from typing import Any, Protocol, runtime_checkable


class SourceError(Exception):
    """來源資料庫讀不到、表不存在、或結構不符預期。載入階段把它轉成 E-4001。"""


def _quote_identifier(name: str) -> str:
    """把識別字（表名／欄位名）安全地包成 SQL 的雙引號識別字。

    呼叫端在組進 SQL 字串之前都先過白名單（`_check_table`／`tables()` 的
    結果），但白名單只判斷「這個名字存在於來源資料庫」，不判斷名字本身
    好不好組進 SQL 字串——SQLite 合法地允許表名／欄位名本身含雙引號。
    若不在這裡跳脫，即使名字通過了白名單，組字串那一步仍然會炸出使用者
    看不懂的 `sqlite3.OperationalError`，等於白名單檢查白做了一半。
    """
    return '"' + name.replace('"', '""') + '"'


@runtime_checkable
class SourceReader(Protocol):
    """載入階段讀來源資料所需的最小介面。

    Task 7／10／11 依賴這五個方法的確切簽章。開源版只有 `SqliteSourceReader`
    這一種實作。

    標了 `@runtime_checkable` 讓 `isinstance(reader, SourceReader)` 可用，
    但這只驗證「方法名都存在」，不驗證參數簽章——`pyproject.toml` 的 dev
    依賴沒有 mypy／pyright，簽章漂移（例如某個實作把 `table` 參數改名成
    `table_name`）不會被任何工具擋下。`tests/pipeline/test_source.py` 另外
    用 `inspect.signature` 逐一比對 Protocol 與實作的參數名稱／順序，兩者
    分工：這裡守「有沒有這個方法」，測試那邊守「簽章長得一不一樣」。
    """

    def tables(self) -> list[str]:
        """來源資料庫裡的所有表名，依名稱排序，不含來源資料庫自己的系統表。

        「哪些算系統表」由各實作依自己的資料庫種類判斷——這個介面不知道也
        不該知道底下是哪一種資料庫。
        """
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
        # 0 bytes 對 SQLite 而言是合法語意——代表一個全新、還沒寫入任何
        # 內容的空資料庫（PRAGMA schema_version 對它一樣會成功，
        # tables() 也會如實回傳 []）。但使用者實際指到的 0-byte 檔案，
        # 十之八九是傳輸中斷、磁碟寫滿、或程式中途被砍掉留下的半成品，
        # 不是刻意建立的空資料庫。若不在這裡點名，使用者接下來只會看到
        # 「表不存在」，被導向去查表名，而看不出真正原因是檔案整個是空的。
        if path.stat().st_size == 0:
            raise SourceError(f"讀不到來源資料庫（檔案是空的）：{path}")
        self._path = path
        con: sqlite3.Connection | None = None
        try:
            # sqlite3.connect() 本身也可能踩到 sqlite3.Error（檔案沒有讀取
            # 權限、被其他行程鎖住、掛載點失效……），與下面的 PRAGMA 探測
            # 共用同一個 try——兩者都要收斂成點名路徑的 SourceError，不能
            # 讓底層例外類別洩漏出去。
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            # sqlite3.connect() 本身是惰性的：對「檔案存在、但內容根本不是
            # SQLite 資料庫」的檔案（改過副檔名的 CSV、半下載的檔案、
            # Git LFS pointer……）一樣會靜默成功回傳連線物件，錯誤要等到
            # 第一次真正查詢時才冒出來，而且冒出來的是使用者看不懂的
            # sqlite3.DatabaseError。PRAGMA schema_version 對任何合法的
            # SQLite 檔案（甚至是全新的空檔案）都能立刻執行，用它在連線
            # 當下就探測一次，把這類錯誤提早轉成點名路徑的 SourceError。
            con.execute("PRAGMA schema_version")
        except sqlite3.Error as exc:
            if con is not None:
                con.close()
            raise SourceError(
                f"讀不到來源資料庫（檔案內容不是有效的 SQLite 資料庫）：{path}"
            ) from exc
        self._con = con

    def _execute(self, sql: str) -> sqlite3.Cursor:
        """查詢的第一步：把 `self._con.execute(sql)` 這一次 step 冒出的
        `sqlite3.Error` 包成 `SourceError`。

        CPython 的 `sqlite3.Connection.execute()` 會在呼叫當下先 step 一次，
        所以「連線當下就能發現」的錯誤在這裡就抓得到。但 SQLite 本身是
        惰性的，後續每疊代一次才 fetch 下一列，真正的錯誤（來源資料庫在
        連線之後才損壞、檔案只有後段被截斷……）常常要疊代到後面才會冒出
        來，發生在這個 try 的範圍之外——那一段由 `_fetch_iter()` 負責收斂，
        兩者合起來才是完整的入口，載入階段（Task 10／11）才能只認
        `SourceError` 這一種例外類型做轉換。
        """
        try:
            return self._con.execute(sql)
        except sqlite3.Error as exc:
            raise SourceError(f"查詢來源資料庫 {self._path} 時發生錯誤：{exc}") from exc

    def _fetch_iter(self, cursor: sqlite3.Cursor) -> Iterator[sqlite3.Row]:
        """查詢的第二步：疊代 `cursor`，把沿途冒出的 `sqlite3.Error` 收斂成
        `SourceError`。

        `_execute()` 只包得住 `execute()` 那一次 step；`fetchall()`／
        `fetchone()`／直接疊代 cursor，底層做的事都是「向 SQLite 要下一列」，
        一樣可能各自踩到 `sqlite3.Error`（另一個行程正在覆寫檔案、大檔案
        只有部分內容毀損……），且往往要疊代到後面幾列才會冒出來。
        `tables()`／`primary_key()`／`columns()`／`count()` 的
        `fetchall()`／`fetchone()`，以及 `rows()` 的逐列疊代，全部改經
        這個疊代器收斂，不再各自裸執行。
        """
        try:
            yield from cursor
        except sqlite3.Error as exc:
            raise SourceError(f"讀取來源資料庫 {self._path} 時發生錯誤：{exc}") from exc

    def close(self) -> None:
        """關閉底層 SQLite 連線。

        Task 10／11 會為每個來源開一個 reader，沒有這個方法連線就只能一路
        開到行程結束。刻意保持最小——不做連線池、不做重複關閉的特殊處理，
        這些都超出目前用得到的範圍。
        """
        self._con.close()

    def __enter__(self) -> SqliteSourceReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def tables(self) -> list[str]:
        rows = list(
            self._fetch_iter(
                self._execute(
                    "SELECT name FROM sqlite_master"
                    " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    " ORDER BY name"
                )
            )
        )
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
        tables = self.tables()
        if table in tables:
            return
        # SQLite 本身比對表名不分大小寫，但這裡的白名單比對是分大小寫的
        # 字串相等——若庫裡是 Ord_Deta、mapping.json 寫成 ord_deta，單純說
        # 「表不存在」會把人導向「表名打錯字」，而看不出真正原因是大小寫
        # 不符。找得到忽略大小寫後相符的表名，就在訊息裡直接點名它；行為
        # 不變，仍然是同一種 SourceError，只是訊息更好懂。
        hint = ""
        case_insensitive_matches = [t for t in tables if t.lower() == table.lower()]
        if case_insensitive_matches:
            hint = f"（大小寫不符：來源資料庫裡的表名是 {case_insensitive_matches[0]!r}）"
        raise SourceError(f"表 {table!r} 不存在於來源資料庫 {self._path}{hint}")

    def primary_key(self, table: str) -> list[str]:
        self._check_table(table)
        # PRAGMA table_info 的 pk 欄位：0 表示不是主鍵，非 0 是複合主鍵裡的
        # 1-based 序位——與欄位在 CREATE TABLE 裡的宣告順序無關，必須依這個
        # 數值排序才是真正的主鍵順序。
        info = list(
            self._fetch_iter(self._execute(f"PRAGMA table_info({_quote_identifier(table)})"))
        )
        pk_columns = sorted((row["pk"], row["name"]) for row in info if row["pk"] > 0)
        return [name for _, name in pk_columns]

    def columns(self, table: str) -> list[str]:
        self._check_table(table)
        info = list(
            self._fetch_iter(self._execute(f"PRAGMA table_info({_quote_identifier(table)})"))
        )
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
        #
        # 代價提醒：有 PK 的表，SQLite 可以直接用 PK 索引滿足這個
        # ORDER BY，沿著索引串流、代價是常數。沒有 PK、退而求其次依全部
        # 欄位排序的表就沒有這種索引可用，SQLite 必須先把整個結果集排進
        # 暫存 b-tree（`EXPLAIN QUERY PLAN` 會顯示 `USE TEMP B-TREE FOR
        # ORDER BY`）才能吐出第一列——實測 20 萬列的無 PK 表，第一列要等
        # 0.124s，有 PK 的表與明寫 `ORDER BY rowid` 的表都是 0.000s。也就是
        # 說「惰性、不整批讀進記憶體」這個保證，在無 PK 的大表上只在
        # Python 這一側成立（不整批讀進 Python 的 list），SQLite 那一側仍然
        # 要先把整個結果集排序完；記憶體用量與首列延遲不是常數，會隨表的
        # 大小成長。這與「完全不寫 ORDER BY」不同：後者要不要用覆蓋索引、
        # 要不要照 rowid 順序，是查詢優化器的實作細節，換版本、換查詢形狀
        # 就可能改變，不是能依賴的契約；明寫 `ORDER BY rowid` 對一般
        # rowid 表則是完全指定的全序，可以沿 b-tree 串流，不是同一回事。
        order_columns = self.primary_key(table) or self.columns(table)
        order_clause = ", ".join(_quote_identifier(column) for column in order_columns)
        cursor = self._execute(f"SELECT * FROM {_quote_identifier(table)} ORDER BY {order_clause}")
        for row in self._fetch_iter(cursor):
            yield dict(row)

    def count(self, table: str) -> int:
        # count() 與 rows() 必須是兩條獨立的取得路徑，刻意不透過疊代
        # 逐列讀取再數長度來實作。否則 rows() 若因為某個缺陷少吐一列，
        # Task 11 的 row_count 檢查會分子（實際載入列數）分母（來源列數）
        # 一起錯，兩個數字剛好對得上而顯示全綠——這正是計劃 2 那個
        # Critical 缺陷的形狀。這個約束不是靠原始碼字面上有沒有出現某個
        # 字串來守（換一個方法名、換一層 getattr 別名都能繞過字串比對），
        # 而是靠 tests/pipeline/test_source.py 側錄真正送進 SQLite 的資料
        # 查詢有幾條來驗證：除了白名單檢查那一條 sqlite_master 查詢，
        # count() 只准再送出剛好一條資料查詢，且那條必須含 COUNT(。
        self._check_table(table)
        cursor = self._execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}")
        return next(self._fetch_iter(cursor))[0]
