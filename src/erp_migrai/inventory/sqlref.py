"""從內嵌 SQL 語句擷取「種類 + 表名 + 行號」。

⚠️ 這是**啟發式**擷取，不是完整的 SQL 文法解析。

為什麼刻意如此：scan 對 SQL 的需求只有種類與涉及的表（見 models.SqlStatement）。
模組四（程式碼轉換）不在 MVP 範圍，因此不需要完整 AST。與其掛一個解析不準的
半套文法假裝嚴謹，不如明確標示這是啟發式——這是本專案的誠實性原則。

已知限制：不解析子查詢、不處理 JOIN 子句的表名、不理解 SQL 字串常數中的關鍵字。
遇到擷取不正確的案例請開 parser-gap-report issue。
"""

from __future__ import annotations

import re

from erp_migrai.inventory.models import SqlStatement

SQL_KEYWORDS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "DECLARE"})

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

_FROM_CLAUSE = re.compile(
    rf"\bFROM\s+(?P<tables>{_IDENT}(?:\s+{_IDENT})?(?:\s*,\s*{_IDENT}(?:\s+{_IDENT})?)*)",
    re.IGNORECASE,
)
_INSERT_INTO = re.compile(rf"\bINSERT\s+INTO\s+(?P<table>{_IDENT})", re.IGNORECASE)
_UPDATE = re.compile(rf"^\s*UPDATE\s+(?P<table>{_IDENT})", re.IGNORECASE)
_DECLARE_CURSOR = re.compile(rf"^\s*DECLARE\s+{_IDENT}\s+CURSOR\s+FOR\b", re.IGNORECASE)
_FIRST_WORD = re.compile(rf"^\s*(?P<word>{_IDENT})")


def _tables_from_from_clause(statement: str) -> list[str]:
    """FROM 子句的表名。允許 `table alias` 形式，別名會被丟掉。"""
    match = _FROM_CLAUSE.search(statement)
    if not match:
        return []
    names: set[str] = set()
    for part in match.group("tables").split(","):
        tokens = part.split()
        if tokens:
            names.add(tokens[0])
    return sorted(names)


def extract_sql(statement: str, line: int) -> SqlStatement | None:
    """回傳 SqlStatement，若該語句不是本模組認得的 SQL 則回傳 None。"""
    first = _FIRST_WORD.match(statement)
    if not first:
        return None
    word = first.group("word").upper()
    if word not in SQL_KEYWORDS:
        return None

    if word == "DECLARE":
        if not _DECLARE_CURSOR.match(statement):
            return None
        return SqlStatement(
            kind="DECLARE_CURSOR",
            tables=_tables_from_from_clause(statement),
            line=line,
        )

    if word == "SELECT":
        return SqlStatement(kind="SELECT", tables=_tables_from_from_clause(statement), line=line)

    if word == "INSERT":
        match = _INSERT_INTO.search(statement)
        return SqlStatement(
            kind="INSERT", tables=[match.group("table")] if match else [], line=line
        )

    if word == "UPDATE":
        match = _UPDATE.match(statement)
        return SqlStatement(
            kind="UPDATE", tables=[match.group("table")] if match else [], line=line
        )

    # DELETE
    return SqlStatement(kind="DELETE", tables=_tables_from_from_clause(statement), line=line)
