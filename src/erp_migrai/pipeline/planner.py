"""載入計畫：把 `mapping.json` 換成「先寫哪張表、每張表怎麼寫」的計畫。

兩件事各自獨立又互相搭配：

1. **external_id 編碼**（`encode_pk_value`／`build_external_id`）——Odoo
   匯入時用 `migrai.<source_id>.<table>.<pk>` 這種 external id 認出「這是
   來源系統的哪一筆記錄」，讓同一份來源資料重複匯入時能認出舊記錄、
   更新而非重複建立。PK 值、`source_id`、`table` 三段都可能含有分隔字元
   本身（`.`／`-`），不逐一跳脫的話，兩筆完全不同的來源資料可能組出
   一模一樣的字串，被悄悄寫成同一筆記錄——這正是「絕不悄悄產生錯的
   輸出」要擋的形狀，細節見 `encode_pk_value`／`build_external_id`
   docstring。

2. **依 reference 相依關係做拓撲排序**（`plan_load`）——`sale.order.line`
   的 `order_id` 要指向 `sale.order` 的某一筆記錄，那筆記錄必須先被寫進
   Odoo、Odoo 才可能已經認得那個 external id。被參照的表沒排在前面，
   匯入時外鍵會指到一筆還不存在的記錄。
"""

from __future__ import annotations

import string
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from erp_migrai.pipeline.source import SourceReader

# external_id 裡「不需要跳脫」的字元集合：ASCII 英數字與底線。
# 分隔字元（`migrai.` 的 `.`、多個 PK 值之間的 `-`）都刻意不在這個集合
# 裡——它們只在「組字串的那一層」（`build_external_id`）當字面值使用；
# 一旦某段內容本身含有這些字元，就必須被編碼掉，否則兩段不同內容可能
# 因為分隔字元的位置不同而組出同一個字串（見模組 docstring）。
_UNRESERVED_BYTES = frozenset(ord(c) for c in string.ascii_letters + string.digits + "_")


def encode_pk_value(value: Any) -> str:
    """把單一值編成 external_id 安全的字串（設計 spec §3.3）。

    做法：先用 `str()` 轉成字串，再用 UTF-8 逐位元組檢查——
    `[A-Za-z0-9_]` 之外的位元組（**含 `%` 自己**）一律編成 `%XX`
    （大寫十六進位）。

    - 逐「位元組」而不是逐「字元」：中文等非 ASCII 值的每個字元本身就是
      多個 UTF-8 位元組，逐位元組編碼才能保證輸出只含 ASCII 安全字元。
    - `%` 自己也要編碼：不編碼的話，字面上寫著 `%2D`（有人手動輸入這串
      當 PK 值）與「值裡有一個 `-` 被編碼成 `%2D`」會組出同一段輸出，
      兩者原本是不同的值卻讀不出差異。
    - 這個函式只負責「單一值」怎麼編碼；分隔字元（`build_external_id`
      裡的 `-`／`.`）留給呼叫端決定要不要跳脫、跳脫哪些字元——
      這裡蓄意保持與「呼叫端要組什麼形狀的字串」無關。
    """
    return "".join(
        chr(byte) if byte in _UNRESERVED_BYTES else f"%{byte:02X}"
        for byte in str(value).encode("utf-8")
    )


def build_external_id(source_id: str, table: str, pk_values: Sequence[Any]) -> str:
    """組出 `migrai.<source_id>.<table>.<編碼後PK>`。

    `source_id`（呼叫端傳入、代表是哪一個來源系統）與 `table`（來自 DDL）
    跟 PK 值一樣過 `encode_pk_value` 同一套編碼，理由完全相同：兩者都不
    保證只含 `[A-Za-z0-9_]`，若不編碼，`.` 在 `source_id`／`table` 裡的
    位置差異可以組出完全相同的字串——

        (source_id="a.b", table="c")   → 不編碼會變成 "migrai.a.b.c.X"
        (source_id="a",   table="b.c") → 不編碼也會變成 "migrai.a.b.c.X"

    對真實輸入（`case03`、`cust_mast` 這類只含 `[A-Za-z0-9_]` 的名字）
    這一步是零成本：編碼後原樣不變，`build_external_id("case03",
    "cust_mast", ["C001"]) == "migrai.case03.cust_mast.C001"` 依然成立。

    多個 PK 值（複合主鍵）之間用 `-` 連接——同樣的道理，值內的 `-` 已經
    在 `encode_pk_value` 那一步被編碼成 `%2D`，這裡的 `-` 才是安全的、
    純粹當分隔字元用的字面值。
    """
    encoded_pk = "-".join(encode_pk_value(value) for value in pk_values)
    return f"migrai.{encode_pk_value(source_id)}.{encode_pk_value(table)}.{encoded_pk}"


@dataclass(frozen=True)
class ColumnPlan:
    """一個來源欄位要怎麼寫進 Odoo。"""

    source_column: str
    # reference 時是 `<field>/id`（Odoo 匯入格式，表示這一格是 external
    # id），其他 kind 原樣是 `<field>`。
    odoo_field: str
    # 整個 transform dict（`kind`／`value_map`／`default`／`value`／
    # `ref_table`／`ref_column`），不只挑 `kind`——下游（Task 8 的
    # `apply_transform`）需要其餘欄位才能真正做值的轉換。
    transform: dict[str, Any]


@dataclass(frozen=True)
class TablePlan:
    """一張來源表的完整載入計畫：對到哪個 Odoo 模型、每個欄位怎麼寫。"""

    source_table: str
    target_model: str
    # 依來源欄位名字典序排序（確定性），見 `plan_load` docstring。
    columns: tuple[ColumnPlan, ...]


class CyclicReference(Exception):
    """來源表之間的 reference 成環。載入階段把它轉成 E-4001。"""


class UnsupportedReference(Exception):
    """被參照表是複合 PK，或沒有對應到任何 Odoo 模型（MVP 未支援）。

    載入階段把它轉成 E-4001。
    """


def _topological_order(edges: dict[str, set[str]]) -> list[str]:
    """Kahn 演算法：每一輪把「所有還沒被排進去、且剩餘依賴已清空」的節點
    一次取出、依表名字典序排序後整批放進結果，藉此讓「同一層」的節點有
    確定性的順序。

    `edges[table]` 是 `table` 依賴的（必須排在它之前的）表集合。若還有
    節點剩下、但沒有任何一個節點的剩餘依賴是空的，代表剩下的節點全部
    互相牽扯成環，直接點名這些表、丟出 `CyclicReference`。
    """
    remaining = {table: set(deps) for table, deps in edges.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(table for table, deps in remaining.items() if not deps)
        if not ready:
            cycle = sorted(remaining)
            raise CyclicReference(
                "來源表之間的 reference 成環，無法決定載入順序：" + "、".join(cycle)
            )
        for table in ready:
            order.append(table)
            del remaining[table]
        for deps in remaining.values():
            deps -= set(ready)
    return order


def _odoo_field(field_mapping: dict[str, Any]) -> str:
    field_name = field_mapping["target"]["field"]
    if field_mapping["transform"]["kind"] == "reference":
        return f"{field_name}/id"
    return field_name


def plan_load(
    mapping: dict[str, Any],
    reader: SourceReader,
    *,
    source_id: str,
) -> list[TablePlan]:
    """依 reference 的相依關係做拓撲排序，被參照者排前面。

    節點是 `mapping["models"]` 裡的每個 `source_table`；邊是每個
    `kind == "reference"` 的 transform：`source_table` 依賴 `ref_table`
    （`ref_table` 必須先被寫進 Odoo）。同層之間依表名字典序排序，確保
    同樣的輸入永遠得到同樣的順序。

    每張表底下的 `ColumnPlan` 依來源欄位名字典序排序，同樣是為了
    確定性——`mapping["mappings"]` 這份 dict 的原始順序不是這裡承諾的
    契約。

    `source_id` 目前只用來組 external_id（`build_external_id`），不會
    出現在 `mapping.json` 或 `inventory.json` 裡——它是 Task 10 的 CLI
    呼叫端才知道的「這次載入是哪一個來源系統」。

    兩種被參照表的問題會在拓撲排序**之前**就攔下來（用不到排序結果）：

    - `ref_table` 不在 `mapping["models"]` 裡：沒有對應到任何 Odoo
      模型，無法解析外鍵 → `UnsupportedReference`。
    - `ref_table` 的 PK（由 `reader.primary_key()` 查得）長度 > 1：
      複合主鍵，MVP 未支援用複合鍵組 external_id（用部分鍵湊合會安靜地
      指到錯誤的記錄）→ `UnsupportedReference`。
    """
    table_to_model = {model["source_table"]: model["target_model"] for model in mapping["models"]}
    field_mappings: list[dict[str, Any]] = mapping.get("mappings", [])

    columns_by_table: dict[str, list[dict[str, Any]]] = {table: [] for table in table_to_model}
    edges: dict[str, set[str]] = {table: set() for table in table_to_model}

    for field_mapping in field_mappings:
        table = field_mapping["source"]["table"]
        columns_by_table[table].append(field_mapping)

        transform = field_mapping["transform"]
        if transform["kind"] != "reference":
            continue

        ref_table = transform["ref_table"]
        if ref_table not in table_to_model:
            raise UnsupportedReference(
                f"{table!r} 參照的來源表 {ref_table!r} 沒有對應到任何 Odoo 模型，無法解析外鍵"
            )
        if len(reader.primary_key(ref_table)) > 1:
            raise UnsupportedReference(
                f"被參照的來源表 {ref_table!r} 是複合主鍵，MVP 未支援這種外鍵形狀"
            )
        edges[table].add(ref_table)

    order = _topological_order(edges)

    plans = []
    for table in order:
        columns = tuple(
            ColumnPlan(
                source_column=field_mapping["source"]["column"],
                odoo_field=_odoo_field(field_mapping),
                transform=field_mapping["transform"],
            )
            for field_mapping in sorted(
                columns_by_table[table], key=lambda field_mapping: field_mapping["source"]["column"]
            )
        )
        plans.append(
            TablePlan(source_table=table, target_model=table_to_model[table], columns=columns)
        )
    return plans
