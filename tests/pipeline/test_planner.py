"""`pipeline/planner.py` 的行為契約：external_id 編碼與載入計畫的拓撲排序。

## 為什麼 `source_id`／`table` 也要過 `encode_pk_value`

任務書原本只要求 PK 值本身編碼、逐位元組跳脫分隔用的 `-`。但
`build_external_id` 是 `f"migrai.{source_id}.{table}.{編碼後PK}"`——
`source_id`（呼叫端傳入的字串）與 `table`（來自 DDL）同樣可能含 `.`，
而 `.` 正是 `migrai.` 與三個欄位之間的分隔字元。不編碼的話：

    (source_id="a.b", table="c") → "migrai.a.b.c.X"
    (source_id="a",   table="b.c") → "migrai.a.b.c.X"

兩者組出完全相同的字串——這與任務書原本要擋的 PK 碰撞是同一個理由，
只是換了一段字串。`test_build_external_id_encodes_source_id_and_table_
to_close_the_same_collision_shape` 專門守這個缺口。

## 拓撲排序 fixture 的設計

`_mapping_with_case03_shape()` 的 `models` 刻意排成
`["ord_deta", "ord_mast", "prod_mast", "cust_mast"]`——不是字典序
（`cust_mast < ord_deta < ord_mast < prod_mast`），也不是正確的拓撲序
（`cust_mast, prod_mast, ord_mast, ord_deta`）。這樣「照 dict／list
原始順序輸出」與「真正做拓撲排序＋同層依字典序排序」這兩種實作，在這份
fixture 上才分辨得出來——若表名字典序剛好等於拓撲序，兩種實作永遠得到
同一個答案，測試測不出真正的差異（計劃 1／5 都踩過這個坑）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from erp_migrai.pipeline.planner import (
    ColumnPlan,
    CyclicReference,
    TablePlan,
    UnsupportedReference,
    build_external_id,
    encode_pk_value,
    plan_load,
)

# --------------------------------------------------------------------------
# 測試夾具：來源資料的 reader 與 mapping.json 的最小 dict 形狀。
#
# 這些不是 fake Odoo client——`plan_load` 的輸入本來就是「mapping.json
# 這個 dict」與「SourceReader 這個來源資料介面」，與 Odoo 完全無關
# （任務書 Global Constraints 第 2 條的界線）。
# --------------------------------------------------------------------------


class _FixedPkReader:
    """對外只需要回答「這張表的主鍵有幾欄」，不做真正的資料查詢。

    `plan_load` 只會呼叫 `primary_key()`（判斷被參照表是不是複合主鍵），
    測試用不到 `tables()`／`columns()`／`rows()`／`count()`，但仍然照
    `SourceReader` 的簽章把五個方法都寫全，維持這個類別是一個「長得像
    SourceReader」的最小夾具，而不是半殘的替身。
    """

    def __init__(self, composite_pk_tables: set[str] | None = None) -> None:
        self._composite_pk_tables = composite_pk_tables or set()

    def tables(self) -> list[str]:
        return []

    def primary_key(self, table: str) -> list[str]:
        if table in self._composite_pk_tables:
            return [f"{table}_k1", f"{table}_k2"]
        return [f"{table}_pk"]

    def columns(self, table: str) -> list[str]:
        return []

    def rows(self, table: str) -> Iterator[dict[str, Any]]:
        return iter(())

    def count(self, table: str) -> int:
        return 0


def _reader_with_single_column_pks() -> _FixedPkReader:
    return _FixedPkReader()


def _reader_with_composite_pk(table: str) -> _FixedPkReader:
    return _FixedPkReader(composite_pk_tables={table})


def _identity(table: str, column: str, model: str, field: str) -> dict[str, Any]:
    return {
        "source": {"table": table, "column": column, "type": "VARCHAR(60)"},
        "target": {"model": model, "field": field, "type": "char"},
        "transform": {
            "kind": "identity",
            "value_map": None,
            "default": None,
            "value": None,
            "ref_table": None,
            "ref_column": None,
        },
    }


def _reference(
    table: str, column: str, model: str, field: str, ref_table: str, ref_column: str
) -> dict[str, Any]:
    return {
        "source": {"table": table, "column": column, "type": "CHAR(16)"},
        "target": {"model": model, "field": field, "type": "many2one"},
        "transform": {
            "kind": "reference",
            "value_map": None,
            "default": None,
            "value": None,
            "ref_table": ref_table,
            "ref_column": ref_column,
        },
    }


def _mapping_with_case03_shape() -> dict[str, Any]:
    """case03 的四個模型與三條 reference，形狀照真實 golden
    （`corpus/case03_sales_order/expected/mapping.json`）。"""
    return {
        "models": [
            {"source_table": "ord_deta", "target_model": "sale.order.line"},
            {"source_table": "ord_mast", "target_model": "sale.order"},
            {"source_table": "prod_mast", "target_model": "product.product"},
            {"source_table": "cust_mast", "target_model": "res.partner"},
        ],
        "mappings": [
            _identity("ord_deta", "ord_desc", "sale.order.line", "name"),
            _reference("ord_deta", "ord_no", "sale.order.line", "order_id", "ord_mast", "ord_no"),
            _reference(
                "ord_deta",
                "ord_prod_no",
                "sale.order.line",
                "product_id",
                "prod_mast",
                "prod_no",
            ),
            _reference(
                "ord_mast", "ord_cust_no", "sale.order", "partner_id", "cust_mast", "cust_no"
            ),
            _identity("ord_mast", "ord_no", "sale.order", "name"),
            _identity("prod_mast", "prod_name", "product.product", "name"),
            _identity("cust_mast", "cust_name", "res.partner", "name"),
        ],
    }


def _mapping_with_cycle() -> dict[str, Any]:
    return {
        "models": [
            {"source_table": "a_tbl", "target_model": "model.a"},
            {"source_table": "b_tbl", "target_model": "model.b"},
        ],
        "mappings": [
            _reference("a_tbl", "b_no", "model.a", "b_id", "b_tbl", "no"),
            _reference("b_tbl", "a_no", "model.b", "a_id", "a_tbl", "no"),
        ],
    }


def _mapping_referencing(ref_table: str) -> dict[str, Any]:
    return {
        "models": [
            {"source_table": ref_table, "target_model": "model.parent"},
            {"source_table": "child_tbl", "target_model": "model.child"},
        ],
        "mappings": [
            _reference("child_tbl", "parent_no", "model.child", "parent_id", ref_table, "no"),
        ],
    }


# --------------------------------------------------------------------------
# encode_pk_value / build_external_id
# --------------------------------------------------------------------------


def test_encode_pk_value_escapes_separator_so_collisions_are_impossible():
    """('A-1','2') 與 ('A','1-2') 必須編出不同的 external_id。

    若沒有這條，兩筆來源資料會被悄悄寫成一筆——「絕不悄悄產生錯的輸出」
    要擋的正是這種形狀。
    """
    a = build_external_id("c3", "t", ["A-1", "2"])
    b = build_external_id("c3", "t", ["A", "1-2"])
    assert a != b


def test_encode_pk_value_escapes_percent_itself():
    """% 不編碼的話，'%2D' 這個字面值會與編碼後的 '-' 撞在一起。"""
    a = build_external_id("c3", "t", ["%2D"])
    b = build_external_id("c3", "t", ["-"])
    assert a != b


def test_build_external_id_shape():
    assert build_external_id("case03", "cust_mast", ["C001"]) == "migrai.case03.cust_mast.C001"


def test_build_external_id_encodes_source_id_and_table_to_close_the_same_collision_shape():
    """`source_id` 與 `table` 也要過 `encode_pk_value`，理由與 PK 值那兩條
    完全一樣：分隔用的 `.` 不編碼的話，把一段內容的 `.` 挪到相鄰欄位，
    可以組出一模一樣的字串。

    `(source_id="a.b", table="c")` 與 `(source_id="a", table="b.c")` 若都不
    編碼，兩者都會組成 `"migrai.a.b.c.X"`——完全相同、卻是兩個不同來源
    表／不同來源系統的資料，會被悄悄寫成同一筆記錄。
    """
    a = build_external_id("a.b", "c", ["X"])
    b = build_external_id("a", "b.c", ["X"])
    assert a != b


def test_encode_pk_value_percent_encodes_non_ascii_bytes():
    """中文等非 ASCII 值要逐位元組編碼，不能囫圇放進 external_id。

    Odoo 的 external_id／XML ID 慣例上只用 ASCII，中文 PK 值（例如台灣
    T100 常見的中文客戶代號）若不編碼，直接嵌進去在部分工具鏈會有編碼
    疑慮；這裡釘住實際的 UTF-8 逐位元組編碼結果。
    """
    assert encode_pk_value("台") == "%E5%8F%B0"


def test_encode_pk_value_handles_non_string_scalars():
    """int／float／None 都要先轉字串才編碼，不能假設呼叫端一定傳字串。

    `float` 值刻意選一個帶小數點的數字：`.` 在編碼規則裡不是安全字元，
    若 `encode_pk_value` 忘記把轉出來的字串也拿去做逐位元組編碼（例如
    偷懶對 `int`／`float` 直接 `str()` 後就回傳、不跑編碼迴圈），這條會
    先抓到。
    """
    assert encode_pk_value(123) == "123"
    assert encode_pk_value(1.5) == "1%2E5"
    assert encode_pk_value(None) == "None"


def test_build_external_id_with_empty_pk_values_does_not_crash():
    """`pk_values` 是空清單時，行為要有定義（結尾留一個空區段），而不是拋例外。

    目前實務上不會發生（PK 至少有一欄），但呼叫端萬一傳空清單，
    大聲失敗的地方應該是別處的驗證邏輯，不該是這裡意外的 `IndexError`
    或 `str.join` 之外的例外。
    """
    assert build_external_id("c3", "t", []) == "migrai.c3.t."


# --------------------------------------------------------------------------
# plan_load：拓撲排序
# --------------------------------------------------------------------------


def test_plan_load_orders_referenced_tables_first():
    """cust_mast / prod_mast 必須排在 ord_mast 之前，ord_mast 在 ord_deta 之前。"""
    plans = plan_load(
        _mapping_with_case03_shape(), _reader_with_single_column_pks(), source_id="c3"
    )
    order = [p.source_table for p in plans]
    assert order.index("cust_mast") < order.index("ord_mast")
    assert order.index("prod_mast") < order.index("ord_deta")
    assert order.index("ord_mast") < order.index("ord_deta")


def test_plan_load_is_deterministic_within_a_layer():
    """同層之間依表名字典序，順序必須等於預期的字典序清單。

    ⚠️ 不寫成「兩次呼叫結果相同」：同一個行程內 dict／list 保序，就算
    拿掉同層排序的 `sorted()`，兩次呼叫仍然會得到同一個（但錯誤的）答案，
    測試永遠不會紅。這裡直接斷言完整的預期順序——
    `cust_mast`／`prod_mast` 這一層沒有互相依賴，正確答案是依字典序排
    `cust_mast` 在前；`_mapping_with_case03_shape()` 的 `models` 刻意不
    照這個順序列，兩種實作在這裡才分辨得出來。
    """
    m, r = _mapping_with_case03_shape(), _reader_with_single_column_pks()
    expected = ["cust_mast", "prod_mast", "ord_mast", "ord_deta"]
    assert [p.source_table for p in plan_load(m, r, source_id="c3")] == expected
    # 兩次呼叫仍然要相同（確定性），但這只是附帶條件，不是唯一的守衛。
    assert [p.source_table for p in plan_load(m, r, source_id="c3")] == expected


def test_cyclic_reference_raises_and_names_every_table_on_the_cycle():
    with pytest.raises(CyclicReference) as exc:
        plan_load(_mapping_with_cycle(), _reader_with_single_column_pks(), source_id="c3")
    assert "a_tbl" in str(exc.value)
    assert "b_tbl" in str(exc.value)


def test_self_reference_raises_cyclic_reference():
    """一張表參照自己（例如物料表的『替代料號』指回同一張表）也算成環。

    這是環的最小形狀（環長度 1），與兩張表互相參照（環長度 2）不是同一條
    程式路徑就能分辨出來——Kahn 演算法若把「入度歸零」誤判為「沒有剩餘
    依賴」而漏算自環，這條會抓到。
    """
    mapping = {
        "models": [{"source_table": "a_tbl", "target_model": "model.a"}],
        "mappings": [
            _reference("a_tbl", "parent_no", "model.a", "parent_id", "a_tbl", "no"),
        ],
    }
    with pytest.raises(CyclicReference) as exc:
        plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")
    assert "a_tbl" in str(exc.value)


def test_multiple_tables_referencing_the_same_table_is_not_a_cycle():
    """兩張表都參照同一張被參照表（菱形依賴），不能被誤判成環。

    `ord_deta` 同時參照 `ord_mast` 與 `prod_mast`（`_mapping_with_case03_
    shape()` 本身就是這個形狀），這裡另外用一個更直接的兩張表都指向同一
    張表的形狀，確認「同一個節點被多個節點依賴」這件事本身不會觸發
    CyclicReference。
    """
    mapping = {
        "models": [
            {"source_table": "parent_tbl", "target_model": "model.parent"},
            {"source_table": "child_a", "target_model": "model.child_a"},
            {"source_table": "child_b", "target_model": "model.child_b"},
        ],
        "mappings": [
            _reference("child_a", "parent_no", "model.child_a", "parent_id", "parent_tbl", "no"),
            _reference("child_b", "parent_no", "model.child_b", "parent_id", "parent_tbl", "no"),
        ],
    }
    order = [
        p.source_table for p in plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")
    ]
    assert order.index("parent_tbl") < order.index("child_a")
    assert order.index("parent_tbl") < order.index("child_b")


def test_isolated_table_with_no_references_appears_in_output():
    """完全沒有 reference、也沒被任何表參照的孤立表，仍然要出現在計畫裡。"""
    mapping = {
        "models": [{"source_table": "standalone_tbl", "target_model": "model.standalone"}],
        "mappings": [],
    }
    plans = plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")
    assert [p.source_table for p in plans] == ["standalone_tbl"]
    assert plans[0].columns == ()


def test_plan_load_with_empty_models_returns_empty_list():
    mapping = {"models": [], "mappings": []}
    assert plan_load(mapping, _reader_with_single_column_pks(), source_id="c3") == []


def test_composite_pk_on_referenced_table_is_rejected_not_guessed():
    """被參照表是複合 PK 時必須大聲失敗，不可以用部分鍵湊合。

    用部分鍵組 external_id 會指到錯誤的記錄，而且錯得很安靜。
    """
    with pytest.raises(UnsupportedReference) as exc:
        plan_load(
            _mapping_referencing("multi_pk_tbl"),
            _reader_with_composite_pk("multi_pk_tbl"),
            source_id="c3",
        )
    assert "multi_pk_tbl" in str(exc.value)


def test_reference_to_table_without_odoo_model_is_rejected():
    """`ref_table` 沒有出現在 `mapping["models"]` 裡（沒對到任何 Odoo 模型）
    時，必須明確拒絕，不能假裝那張表存在。

    這與複合 PK 是兩種不同的『被參照表有問題』：這裡連 `reader.primary_
    key()` 都不該被呼叫得到有意義的答案，因為這張表根本沒有對應的
    Odoo 模型可以承接被解析出來的 id。
    """
    mapping = {
        "models": [{"source_table": "child_tbl", "target_model": "model.child"}],
        "mappings": [
            _reference("child_tbl", "parent_no", "model.child", "parent_id", "ghost_tbl", "no"),
        ],
    }
    with pytest.raises(UnsupportedReference) as exc:
        plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")
    assert "ghost_tbl" in str(exc.value)


def test_reference_transform_becomes_slash_id_field():
    """reference 的 odoo_field 必須是 `<field>/id`——Odoo 匯入用這個路徑
    表示「這一格是 external id，不是原始值」。"""
    plans = plan_load(
        _mapping_with_case03_shape(), _reader_with_single_column_pks(), source_id="c3"
    )
    ord_mast = next(p for p in plans if p.source_table == "ord_mast")
    partner = next(c for c in ord_mast.columns if c.source_column == "ord_cust_no")
    assert partner.odoo_field == "partner_id/id"


def test_non_reference_transform_odoo_field_is_plain_field_name():
    """不是 reference 的欄位，`odoo_field` 就是原封不動的目標欄位名稱，
    不該被加上 `/id`。"""
    plans = plan_load(
        _mapping_with_case03_shape(), _reader_with_single_column_pks(), source_id="c3"
    )
    ord_mast = next(p for p in plans if p.source_table == "ord_mast")
    name = next(c for c in ord_mast.columns if c.source_column == "ord_no")
    assert name.odoo_field == "name"


def test_column_plan_transform_carries_the_whole_transform_dict():
    """`ColumnPlan.transform` 存的是整個 transform dict，不是只挑出 kind。

    下游（Task 8 的 `apply_transform`）需要 `value_map`／`default` 等其他
    欄位，若這裡只搬 `kind` 過去，資訊會在這一層就遺失。
    """
    plans = plan_load(
        _mapping_with_case03_shape(), _reader_with_single_column_pks(), source_id="c3"
    )
    ord_mast = next(p for p in plans if p.source_table == "ord_mast")
    partner = next(c for c in ord_mast.columns if c.source_column == "ord_cust_no")
    assert partner.transform == {
        "kind": "reference",
        "value_map": None,
        "default": None,
        "value": None,
        "ref_table": "cust_mast",
        "ref_column": "cust_no",
    }


def test_table_plan_columns_are_sorted_by_source_column_deterministically():
    """同一張表底下的欄位順序必須是確定性的——依來源欄位名字典序排序。

    fixture 刻意用反字典序輸入（`zeta_col` 先於 `alpha_col`），若實作只是
    原樣保留 `mapping["mappings"]` 裡的輸入順序（沒有排序），這裡會得到
    `["zeta_col", "alpha_col"]` 而不是斷言的字典序，兩種實作才分辨得出來。
    """
    mapping = {
        "models": [{"source_table": "t", "target_model": "model.t"}],
        "mappings": [
            _identity("t", "zeta_col", "model.t", "zeta"),
            _identity("t", "alpha_col", "model.t", "alpha"),
        ],
    }
    plans = plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")
    columns = [c.source_column for c in plans[0].columns]
    assert columns == ["alpha_col", "zeta_col"]


def test_plan_load_returns_table_plan_and_column_plan_instances():
    """回傳型別要真的是任務書指定的 `TablePlan`／`ColumnPlan`，不是巧合同形的
    普通 dict 或 tuple。"""
    plans = plan_load(
        _mapping_with_case03_shape(), _reader_with_single_column_pks(), source_id="c3"
    )
    assert all(isinstance(p, TablePlan) for p in plans)
    assert all(isinstance(c, ColumnPlan) for p in plans for c in p.columns)
