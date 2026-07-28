"""inventory.json 的資料模型。

設計規則：inventory 必須是**確定性**的——不含時間戳、絕對路徑、UUID 或任何
隨機值。同樣輸入必須產生逐位元組相同的 JSON，golden-file 測試才有意義，
也才對得起「可重現」這個對外承諾。時間戳與 run_id 只出現在 events.jsonl。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

SNIPPET_LIMIT = 200


def snippet(text: str, limit: int = SNIPPET_LIMIT) -> str:
    """把原始碼片段壓成單行並截斷，供不支援清單顯示。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


@dataclass
class UnsupportedSyntax:
    """一段本工具無法解析的語法。這是 scan 的一級產出，不是失敗紀錄。"""

    file: str  # 相對於 --src 的路徑
    line: int  # 1 起算
    snippet: str
    reason: str


@dataclass
class Column:
    name: str
    type: str  # 正規化後的型別，例如 "CHAR(10)" / "DECIMAL(15,2)"
    nullable: bool
    default: str | None = None


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


@dataclass
class Table:
    name: str
    columns: list[Column]
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)


@dataclass
class SqlStatement:
    kind: str  # SELECT | INSERT | UPDATE | DELETE | DECLARE_CURSOR
    tables: list[str]
    line: int


@dataclass
class Definition:
    name: str
    type: str  # 正規化後的型別文字，例如 "RECORD LIKE cust_mast.*" / "INTEGER"
    line: int


@dataclass
class ProgramUnit:
    kind: str  # MAIN | FUNCTION | REPORT | GLOBALS
    name: str | None
    file: str
    start_line: int
    end_line: int
    parse_status: str  # ok | partial | failed
    definitions: list[Definition] = field(default_factory=list)
    sql: list[SqlStatement] = field(default_factory=list)
    unsupported: list[UnsupportedSyntax] = field(default_factory=list)


@dataclass
class Inventory:
    synthetic: bool
    schema_version: str
    database: str | None
    source_files: list[str]
    tables: list[Table] = field(default_factory=list)
    programs: list[ProgramUnit] = field(default_factory=list)
    unsupported_syntax: list[UnsupportedSyntax] = field(default_factory=list)
    # 這是在任何正式版本消費 inventory.json 之前新增的欄位：純粹增補，不影響
    # 任何既有消費者的既有欄位，因此不需要調高 SCHEMA_VERSION。放在最後一個
    # 欄位，讓既有欄位順序保持不變。裝的是「其他」被宣告過、但沒有被 database
    # 欄位採用的名稱（已排序、去重）；database 欄位本身受限於只能表達單一名稱
    # 這件事沒有變，這裡只是把原本只留在 events.jsonl 的事實也寫進公開格式。
    database_conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
