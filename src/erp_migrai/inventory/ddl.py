"""從 schema.sql 擷取表結構。

策略與 4GL parser 相同：先在 Python 端把文字切成單一語句，再逐句嘗試用 Lark
解析。解析失敗只記錄成 UnsupportedSyntax 並繼續下一句，絕不中止整份檔案
——這是 scan 的核心行為，也是「不假裝解析成功」的落點。
"""

from __future__ import annotations

import re
from pathlib import Path

from lark import Lark, Transformer
from lark.exceptions import LarkError

from erp_migrai.inventory.models import (
    Column,
    ForeignKey,
    Table,
    UnsupportedSyntax,
    snippet,
)

_GRAMMAR = Lark(
    Path(__file__).with_name("ddl.lark").read_text(encoding="utf-8"),
    parser="lalr",
    start="start",
)

_IS_CREATE_TABLE = re.compile(r"\s*create\s+table\b", re.IGNORECASE)


def split_statements(
    text: str, *, source_file: str
) -> tuple[list[tuple[int, str]], list[UnsupportedSyntax]]:
    """把 SQL 文字切成 (起始行號, 語句) 清單。行號 1 起算，註解已剔除。

    註解剔除與依 `;` 切語句必須是同一個字元級掃描，不能各自獨立用 regex 處理：
    字串常值裡可能藏著 `;`、`--`、`/*` 這些對 regex 而言「看起來像」註解或分隔符
    的字元，唯有邊掃描邊追蹤「目前是否在字串裡」才能正確判斷。掃描器維護五種狀態：
    一般文字、單引號字串、雙引號字串（Informix 預設也接受 `"..."` 當字串常值，
    `''`／`""` 分別是各自常值內逃逸的一個引號，不是結尾——兩種引號的處理完全對稱，
    見下方 I3 的說明）、`--` 行註解（到換行為止）、`/* */` 區塊註解（不支援巢狀）。

    註解一律替換成空白（一個空格，再加上註解內含的換行數），絕不替換成空字串，
    否則像 `INT/**/NOT NULL` 這種寫法會讓 `INT` 與 `NOT` 黏成 `INTNOT`。

    錯誤復原規則（兩者都必須留下痕跡，見下）：
    未終止的字串常值（單引號或雙引號皆同），在找到對應的結尾引號前先遇到換行
    （或掃到檔尾），視為未終止，直接在該處恢復掃描——換行字元本身留給外層迴圈
    當一般字元處理。這把受影響的範圍限制在「到下一個 `;` 為止」，而不是「到
    檔尾為止」，讓同一份檔案裡後面的有效語句仍然能被切出來。未終止的 `/* */`
    區塊註解比照辦理：先在全文找 `*/`，找得到就是正常的（可能跨行的）區塊註解，
    行為與過去相同；找不到才是真正未終止，此時只在這一行內找換行做復原點，
    不會像過去那樣直接吃到檔尾。
    每次這樣的復原都會附帶回傳一筆 UnsupportedSyntax，記錄未終止構件開始的行號，
    讓「資料被裁掉」這件事本身可見，而不是悄悄消失。

    I3（branch review）：雙引號字串常值一開始沒有對應的掃描分支，只有 `'...'`
    這一種。Informix（T100 的資料庫）預設同時接受 `"..."` 當字串常值，這是這個
    分支不一致的落點——`masking.py` 對 `'` 與 `"` 是對稱處理的，`ddl.py` 原本
    沒有對稱：字串裡的 `;` 會把語句切斷、字串裡的 `--` 會把同一行剩下內容當成
    註解吃掉、字串裡的 `/*` 會觸發一個根本不存在的「區塊註解未終止」假報告。
    修正後兩種引號共用同一段掃描邏輯（只是引號字元不同），行為對稱、復原規則
    也對稱。
    """
    out: list[tuple[int, str]] = []
    unsupported: list[UnsupportedSyntax] = []
    buf: list[str] = []
    line = 1
    start_line = 1
    seen_content = False
    n = len(text)
    i = 0

    def note_start(ch: str) -> None:
        nonlocal start_line, seen_content
        if not seen_content and not ch.isspace():
            seen_content = True
            start_line = line

    while i < n:
        ch = text[i]

        if ch == "'" or ch == '"':
            # 字串常值：逐字掃到對應的結尾引號（單引號或雙引號），連續兩個相同的
            # 引號字元（`''` 或 `""`）是常值內的一個引號而非結尾——Informix（T100
            # 的資料庫）預設同時接受這兩種引號的字串常值，`masking.py` 對兩者是
            # 對稱處理的，這裡的字元級掃描器必須跟它一致，不能只認得單引號。
            # 若在找到結尾引號之前先遇到換行，視為未終止，在行尾恢復掃描（換行
            # 字元留給外層迴圈當一般字元處理），並記一筆 UnsupportedSyntax。
            quote = ch
            note_start(ch)
            quote_open_line = line
            quote_start = i
            buf.append(ch)
            i += 1
            closed = False
            while i < n:
                c2 = text[i]
                if c2 == quote:
                    if i + 1 < n and text[i + 1] == quote:
                        buf.append(quote * 2)
                        i += 2
                        continue
                    buf.append(quote)
                    i += 1
                    closed = True
                    break
                if c2 == "\n":
                    break
                buf.append(c2)
                i += 1
            if not closed:
                quote_label = "單引號" if quote == "'" else "雙引號"
                unsupported.append(
                    UnsupportedSyntax(
                        file=source_file,
                        line=quote_open_line,
                        snippet=snippet(text[quote_start:i]),
                        reason=f"{quote_label}字串未終止（缺少結尾 {quote}），已在行尾恢復掃描",
                    )
                )
            continue

        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            # 行註解，掃到換行為止；換行本身留給下一輪當一般字元處理。
            j = i + 2
            while j < n and text[j] != "\n":
                j += 1
            buf.append(" ")
            i = j
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            # 區塊註解，不支援巢狀。先在全文找結尾 */：找得到就是正常的（可能跨行
            # 的）區塊註解，行為與過去完全相同；找不到才是真正未終止，此時只在
            # 「這一行」內找換行做復原點，不能像過去那樣直接吃到檔尾，否則後面
            # 整份檔案的有效語句都會被吞掉。
            comment_open_line = line
            comment_start = i
            j = i + 2
            end = text.find("*/", j)
            if end != -1:
                comment_body = text[j:end]
                i = end + 2
                newline_count = comment_body.count("\n")
                buf.append(" ")
                buf.append("\n" * newline_count)
                line += newline_count
                continue
            k = text.find("\n", j)
            recover = k if k != -1 else n
            unsupported.append(
                UnsupportedSyntax(
                    file=source_file,
                    line=comment_open_line,
                    snippet=snippet(text[comment_start:recover]),
                    reason="區塊註解未終止（缺少 */），已在行尾恢復掃描",
                )
            )
            buf.append(" ")
            i = recover
            continue

        if ch == ";":
            body = "".join(buf).strip()
            if body:
                out.append((start_line, body))
            buf = []
            seen_content = False
            i += 1
            continue

        note_start(ch)
        buf.append(ch)
        if ch == "\n":
            line += 1
        i += 1

    body = "".join(buf).strip()
    if body:
        out.append((start_line, body))
    return out, unsupported


class _DdlTransformer(Transformer):
    """把語法樹轉成 Table。回傳型別刻意用 tuple 標記種類，讓組裝邏輯單純。"""

    def ident(self, children):
        return str(children[0])

    def table_name(self, children):
        return children[0]

    def ident_list(self, children):
        return list(children)

    def type_spec(self, children):
        base = str(children[0]).upper()
        nums = [str(c) for c in children[1:]]
        return f"{base}({','.join(nums)})" if nums else base

    def default_val(self, children):
        # 兩種引號都有可能：值本身只會是其中一種，strip() 移除頭尾任一種引號
        # 字元對非字串值（NUMBER／ident）沒有影響，因為那些值裡本來就不含引號。
        return str(children[0]).strip("'\"")

    def default_null(self, _children):
        return "NULL"

    def opt_not_null(self, _children):
        return ("not_null", None)

    def opt_null(self, _children):
        return ("null", None)

    def opt_default(self, children):
        return ("default", children[0])

    def opt_inline_pk(self, _children):
        return ("inline_pk", None)

    def column_def(self, children):
        name, type_spec, *options = children
        nullable = True
        default = None
        inline_pk = False
        for kind, value in options:
            if kind == "not_null":
                nullable = False
            elif kind == "null":
                nullable = True
            elif kind == "default":
                default = value
            elif kind == "inline_pk":
                nullable = False
                inline_pk = True
        column = Column(name=name, type=type_spec, nullable=nullable, default=default)
        return ("column", column, inline_pk)

    def primary_key(self, children):
        return ("pk", children[0])

    def foreign_key(self, children):
        columns, ref_table, ref_columns = children
        return (
            "fk",
            ForeignKey(columns=columns, ref_table=ref_table, ref_columns=ref_columns),
        )

    def element(self, children):
        return children[0]

    def create_table(self, children):
        name, *elements = children
        columns: list[Column] = []
        primary_key: list[str] = []
        inline_pk: list[str] = []
        foreign_keys: list[ForeignKey] = []
        for element in elements:
            if element[0] == "column":
                _, column, is_pk = element
                columns.append(column)
                if is_pk:
                    inline_pk.append(column.name)
            elif element[0] == "pk":
                primary_key = element[1]
            elif element[0] == "fk":
                foreign_keys.append(element[1])
        return Table(
            name=name,
            columns=columns,
            primary_key=primary_key or inline_pk,
            foreign_keys=foreign_keys,
        )

    def start(self, children):
        return children[0]


def extract_tables(text: str, *, source_file: str) -> tuple[list[Table], list[UnsupportedSyntax]]:
    """回傳 (依表名排序的 Table 清單, 不支援語句清單)。

    不支援清單裡，掃描器自己發現的異常（未終止字串／未終止區塊註解）排在最前面，
    後面才是逐句解析失敗或非 CREATE TABLE 的紀錄——因為 `split_statements` 是先
    掃描整份文字，才把切好的語句交回來逐一解析。
    """
    tables: list[Table] = []
    statements, unsupported = split_statements(text, source_file=source_file)

    for line_no, statement in statements:
        if not _IS_CREATE_TABLE.match(statement):
            unsupported.append(
                UnsupportedSyntax(
                    file=source_file,
                    line=line_no,
                    snippet=snippet(statement),
                    reason="非 CREATE TABLE 語句，MVP 的 DDL 擷取不處理",
                )
            )
            continue
        try:
            tree = _GRAMMAR.parse(statement)
            # transform 必須留在 try 內：Lark 會把 Transformer callback 拋出的例外包成
            # VisitError（LarkError 的子類別）。若搬到 try 外，「絕不中止整份檔案」這個
            # 不變量就只靠文法與 Transformer 剛好同步來維持；ddl.lark 是預期外部貢獻者
            # 會擴充的檔案，兩者遲早會不同步，屆時例外就會逃出並中止整份檔案的掃描。
            table = _DdlTransformer().transform(tree)
        except LarkError as exc:
            unsupported.append(
                UnsupportedSyntax(
                    file=source_file,
                    line=line_no,
                    snippet=snippet(statement),
                    reason=f"CREATE TABLE 解析失敗：{type(exc).__name__}：{snippet(str(exc))}",
                )
            )
            continue
        tables.append(table)

    tables.sort(key=lambda t: t.name)
    return tables, unsupported
