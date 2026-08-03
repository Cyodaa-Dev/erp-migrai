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

⚠️ 上面那段推理只證明了「兩段都不編碼會撞」，**推不出「兩段都必須編碼」**：
只要 PK 值本身已編碼（`enc(pk)` 不含 `.`），`source_id`／`table` 之中
**任一個**有編碼就足以杜絕碰撞——字串可以從右邊唯一地切分。兩段都編碼是
**雙保險**（對稱、不必記住哪一個可以不編碼），是正確的做法，但理由要寫對，
否則下一個人會依錯誤推理做決策。完整論證見 `build_external_id` 的 docstring。

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
    _SEPARATOR,
    _UNRESERVED_BYTES,
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
    """對外只需要回答「這張表的主鍵有幾欄、叫什麼名字」，不做真正的資料
    查詢。

    `plan_load` 只會呼叫 `primary_key()`（判斷被參照表是不是複合主鍵、
    計畫裡的表是不是完全沒有主鍵、`ref_column` 是否等於實際的主鍵欄
    名），測試用不到 `tables()`／`columns()`／`rows()`／`count()`，但仍然
    照 `SourceReader` 的簽章把五個方法都寫全，維持這個類別是一個「長得像
    SourceReader」的最小夾具，而不是半殘的替身。

    三種可設定的表：
    - `no_pk_tables`：`primary_key()` 回傳空清單（真實 T100 常見，見
      `source.py` 的 `_iter_rows` 註解）。
    - `composite_pk_tables`：`primary_key()` 回傳兩個欄位（模擬複合
      主鍵，欄名與表名綁定，`_k1`／`_k2`）。
    - `single_pk_names`：`primary_key()` 回傳指定的單一欄名，讓測試能
      控制「PK 欄名」與 mapping 裡宣告的 `ref_column` 是否一致（C3）。
      沒有特別指定的表，預設回傳 `["no"]`——這個名字本身沒有意義，只是
      跟大多數測試 fixture 裡 `_reference(...)` 呼叫時填的 `ref_column`
      對齊，讓那些不是在測 PK 欄名比對本身的測試不需要逐一指定。
    """

    def __init__(
        self,
        composite_pk_tables: set[str] | None = None,
        no_pk_tables: set[str] | None = None,
        single_pk_names: dict[str, str] | None = None,
    ) -> None:
        self._composite_pk_tables = composite_pk_tables or set()
        self._no_pk_tables = no_pk_tables or set()
        self._single_pk_names = single_pk_names or {}

    def tables(self) -> list[str]:
        return []

    def primary_key(self, table: str) -> list[str]:
        if table in self._no_pk_tables:
            return []
        if table in self._composite_pk_tables:
            return [f"{table}_k1", f"{table}_k2"]
        return [self._single_pk_names.get(table, "no")]

    def columns(self, table: str) -> list[str]:
        return []

    def rows(self, table: str) -> Iterator[dict[str, Any]]:
        return iter(())

    def count(self, table: str) -> int:
        return 0


def _reader_with_single_column_pks() -> _FixedPkReader:
    """`_mapping_with_case03_shape()` 用的 reader：被參照表的 PK 欄名要
    跟 golden mapping 裡宣告的 `ref_column` 一致（`ord_mast.ord_no`／
    `prod_mast.prod_no`／`cust_mast.cust_no`），否則加了 C3 的檢查
    （`ref_column` 必須等於實際 PK 欄名）之後，這些原本合法的 reference
    會被誤擋。其餘表（`a_tbl`／`b_tbl`／`parent_tbl` 這類跟 PK 欄名比對
    無關的 fixture）用預設的 `"no"`。
    """
    return _FixedPkReader(
        single_pk_names={"ord_mast": "ord_no", "prod_mast": "prod_no", "cust_mast": "cust_no"}
    )


def _reader_with_composite_pk(table: str) -> _FixedPkReader:
    return _FixedPkReader(composite_pk_tables={table})


def _reader_with_no_primary_key(table: str) -> _FixedPkReader:
    return _FixedPkReader(no_pk_tables={table})


def _reader_with_mismatched_pk_name(table: str, actual_pk: str) -> _FixedPkReader:
    """`table` 的真實 PK 欄名是 `actual_pk`，用來測 C3：mapping 宣告的
    `ref_column` 若不是這個名字，必須被擋下。"""
    return _FixedPkReader(single_pk_names={table: actual_pk})


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
    """`models` 刻意用反字典序（`b_tbl` 先於 `a_tbl`）——`remaining` 的
    插入順序跟著 `models` 走，若字典序恰好等於插入順序，`sorted(remaining)`
    與 `list(remaining)`（不排序）會巧合地給出同一個答案，測不出
    `CyclicReference` 訊息裡的表名順序是不是真的有依字典序排序（Mi4）。
    """
    return {
        "models": [
            {"source_table": "b_tbl", "target_model": "model.b"},
            {"source_table": "a_tbl", "target_model": "model.a"},
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


def test_separator_is_not_in_the_unreserved_byte_set():
    """分隔字元(`_SEPARATOR`)必須不在 `_UNRESERVED_BYTES` 裡——這是
    「分隔字元與值本身的編碼結果不可能撞在一起」的根本不變式，換成任何
    在 unreserved 集合裡的字元（例如 `_`）都會讓分隔字元本身變得含糊：
    `["A_1", "2"]` 與 `["A", "1_2"]` 若用 `_` 當分隔字元，兩者都會組成
    `"A_1_2"`，因為 `_` 既是安全字元又是分隔字元，無法唯一切分。

    這條測試守的是「分隔字元的選擇」這個決策本身，而不是某個寫死的字面
    值——分隔字元換成別的字元也一樣要滿足這條，測試才會跟著換而不是繼續
    硬編碼 `-`。
    """
    assert ord(_SEPARATOR) not in _UNRESERVED_BYTES


def test_separator_is_not_the_percent_escape_prefix():
    """分隔字元不能是 `%`——那是 `encode_pk_value` 逐位元組跳脫序列
    （`%XX`）自己的前導字元，兩者若相同，組出來的字串會沒辦法唯一還原
    （沒辦法分辨一個 `%` 是分隔字元本身、還是某個位元組被跳脫成 `%XX`
    的開頭）。這條不變式與「分隔字元不在 unreserved 集合裡」是兩件事：
    `%` 本身也不在 unreserved 集合裡，`test_separator_is_not_in_the_
    unreserved_byte_set` 擋不住把分隔字元換成 `%` 這個選擇。
    """
    assert _SEPARATOR != "%"


def test_encode_pk_value_escapes_separator_so_collisions_are_impossible():
    """`[f"A{_SEPARATOR}1", "2"]` 與 `["A", f"1{_SEPARATOR}2"]` 必須編出
    不同的 external_id。

    若沒有這條，兩筆來源資料會被悄悄寫成一筆——「絕不悄悄產生錯的輸出」
    要擋的正是這種形狀。動態用 `_SEPARATOR` 組值而不是寫死 `"A-1"`／
    `"1-2"`，是為了讓這條測試跟著「分隔字元是什麼」這個決策走，而不是
    只守住 `-` 這一個字面值（見 `test_separator_is_not_in_the_unreserved_
    byte_set`）。
    """
    a = build_external_id("c3", "t", [f"A{_SEPARATOR}1", "2"])
    b = build_external_id("c3", "t", ["A", f"1{_SEPARATOR}2"])
    assert a != b


def test_encode_pk_value_escapes_percent_itself():
    """% 不編碼的話，'%2D' 這個字面值會與編碼後的 '-' 撞在一起。"""
    a = build_external_id("c3", "t", ["%2D"])
    b = build_external_id("c3", "t", ["-"])
    assert a != b


def test_build_external_id_shape():
    assert build_external_id("case03", "cust_mast", ["C001"]) == "migrai.case03.cust_mast.C001"


def test_build_external_id_composite_key_shape_is_a_backward_compatibility_contract():
    """複合主鍵的 external_id 字面形狀，釘住的是**回溯相容性**，不是內部細節。

    external_id 存在的理由，就是讓同一份來源資料再匯入一次時 Odoo 認得出
    「這是上次那筆」而更新、不是重複建立。一旦 Task 10／11 把這些字串寫進
    Odoo 的 `ir.model.data` 與 `migration_log.jsonl`，字面形式就變成對外
    契約——改掉 `_SEPARATOR`（例如從 `-` 換成 `~`）不會讓任何行為測試變紅
    （那是合法的等價改寫），卻會讓下一次匯入把整批舊記錄當成新記錄再建一份。

    所以這條刻意寫死字面值，與
    `test_separator_is_not_in_the_unreserved_byte_set` 分工：那條守「分隔
    字元的選擇必須安全」，這條守「選定之後不可以再改」。
    """
    assert build_external_id("case03", "ord_deta", ["O0001", 1]) == (
        "migrai.case03.ord_deta.O0001-1"
    )


def test_build_external_id_encodes_source_id_and_table_to_close_the_same_collision_shape():
    """`source_id` 與 `table` 也要過 `encode_pk_value`。

    釘住確切輸出，而不是只比較兩者不相等——只比 `!=` 守不住「兩段都真的
    被編碼了」，換成只編碼其中一段的實作一樣能讓 `a != b` 通過。

    理由：只要 PK 值本身已編碼，`source_id`／`table` 兩段之中任一個有
    編碼就足以杜絕碰撞（見 `build_external_id` docstring 的完整推理）；
    這裡兩段都編碼，測試釘住兩段各自的編碼結果。
    """
    a = build_external_id("a.b", "c", ["X"])
    b = build_external_id("a", "b.c", ["X"])
    assert a == "migrai.a%2Eb.c.X"
    assert b == "migrai.a.b%2Ec.X"
    assert a != b


def test_encode_pk_value_percent_encodes_non_ascii_bytes():
    """中文等非 ASCII 值要逐位元組編碼，不能囫圇放進 external_id。

    Odoo 的 external_id／XML ID 慣例上只用 ASCII，中文 PK 值（例如台灣
    T100 常見的中文客戶代號）若不編碼，直接嵌進去在部分工具鏈會有編碼
    疑慮；這裡釘住實際的 UTF-8 逐位元組編碼結果。
    """
    assert encode_pk_value("台") == "%E5%8F%B0"


def test_encode_pk_value_handles_non_string_scalars():
    """int／float 都要先轉字串才編碼，不能假設呼叫端一定傳字串。

    `float` 值刻意選一個帶小數點的數字：`.` 在編碼規則裡不是安全字元，
    若 `encode_pk_value` 忘記把轉出來的字串也拿去做逐位元組編碼（例如
    偷懶對 `int`／`float` 直接 `str()` 後就回傳、不跑編碼迴圈），這條會
    先抓到。
    """
    assert encode_pk_value(123) == "123"
    assert encode_pk_value(1.5) == "1%2E5"


def test_encode_pk_value_rejects_none():
    """`None` 不是合法的 PK 值，必須大聲拒絕，而不是編成字面上的 "None"。

    上一輪把 `encode_pk_value(None) == "None"` 釘成契約——但這正是問題
    本身：一筆「PK 是 NULL 外鍵」的來源資料與另一筆「PK 剛好是字串
    'None'」的來源資料會被悄悄組成同一個 external_id。NULL 外鍵在 T100
    很常見（客戶未指定、上層單號留空），呼叫端（Task 10）必須在餵值之前
    自行判斷並跳過該格，不能讓這裡安靜地放行。
    """
    with pytest.raises(ValueError):
        encode_pk_value(None)


def test_build_external_id_rejects_empty_pk_values():
    """`pk_values` 是空清單時必須大聲拒絕，而不是組出退化字串。

    上一輪把 `build_external_id("c3", "t", []) == "migrai.c3.t."` 釘成
    契約，前提寫著「PK 至少有一欄，目前實務上不會發生」——但 Task 6 的
    `test_primary_key_returns_empty_list_when_table_has_no_primary_key`
    證明這個前提是假的，真實 T100 會有沒有宣告主鍵的表。空 pk_values
    組出的字串結尾留一個空區段，同一張沒有主鍵的表的每一列都會撞成
    同一個 external_id——這裡改成大聲拒絕，逼呼叫端在更早的地方
    （`plan_load` 對「表沒有宣告主鍵」的檢查）就擋下來。
    """
    with pytest.raises(ValueError):
        build_external_id("c3", "t", [])


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
    """`a_tbl` 與 `b_tbl` 都要出現在訊息裡，而且**依表名字典序**出現。

    只用 `in` 做子字串比對抓不到「表名順序穩不穩定」這件事——`sorted(
    remaining)` 換成 `list(remaining)` 一樣能讓兩個 `in` 斷言通過。這裡
    直接比對「a_tbl 出現的位置在 b_tbl 之前」，把順序也釘住。
    """
    with pytest.raises(CyclicReference) as exc:
        plan_load(_mapping_with_cycle(), _reader_with_single_column_pks(), source_id="c3")
    message = str(exc.value)
    assert "a_tbl" in message
    assert "b_tbl" in message
    assert message.index("a_tbl") < message.index("b_tbl")


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


def test_plan_load_requires_models_key_present():
    """Mi5：`mapping["models"]` 必須用直接索引，不能用 `.get(..., [])`
    默默把缺鍵當成空清單。

    `mapping.schema.json` 的 `required` 已經保證 `models`／`mappings`
    兩個鍵一定存在；若真的缺了，代表上游壞了，應該讓一個明確的
    `KeyError` 冒出來，而不是被 `.get()` 的預設值悄悄吞掉、變成
    「這次匯入完全沒有任何表」這種更難追查的靜默結果。
    """
    mapping = {"mappings": []}
    with pytest.raises(KeyError):
        plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")


def test_plan_load_requires_mappings_key_present():
    """Mi5：同上，`mapping["mappings"]` 也必須用直接索引，不能用 `.get`。"""
    mapping = {"models": []}
    with pytest.raises(KeyError):
        plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")


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
    message = str(exc.value)
    assert "multi_pk_tbl" in message
    # Mi2：這條訊息與「ref_table 不在 models」那條訊息互換仍然能讓舊測試
    # 通過（都只比對表名子字串）——這裡多釘住關鍵詞，區分兩種不同的
    # UnsupportedReference。
    assert "複合主鍵" in message


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
    message = str(exc.value)
    assert "ghost_tbl" in message
    assert "沒有對應到任何 Odoo 模型" in message


# --------------------------------------------------------------------------
# plan_load：每一張表都要有主鍵（C1／C2），被參照表的 ref_column 要對得上
# 實際的主鍵欄名（C3）
# --------------------------------------------------------------------------


def test_referenced_table_without_primary_key_is_rejected_not_silently_collapsed():
    """C1：被參照表沒有宣告主鍵時，必須大聲拒絕。

    修正前的實作只擋 `len(pk) > 1`，漏了 `len(pk) == 0`——這不是假設性的
    邊界情況：`source.py` 的
    `test_primary_key_returns_empty_list_when_table_has_no_primary_key`
    釘住這是真實 T100 會遇到的情況。若不擋，`build_external_id(source_id,
    ref_table, [])` 會對每一列都組出同一個結尾留空的 external_id，整張
    被參照表在下游會塌成一筆——比複合主鍵沒被擋下更糟：複合主鍵好歹每列
    的 external_id 各自不同（雖然不完整），沒有主鍵是連「各自不同」都
    做不到。
    """
    mapping = _mapping_referencing("no_pk_tbl")
    with pytest.raises(UnsupportedReference) as exc:
        plan_load(mapping, _reader_with_no_primary_key("no_pk_tbl"), source_id="c3")
    message = str(exc.value)
    assert "no_pk_tbl" in message
    assert "沒有宣告主鍵" in message


def test_every_table_in_the_plan_must_have_a_primary_key_even_if_never_referenced():
    """C2：沒有主鍵的表就算完全沒被任何人參照，一樣要被擋下。

    修正前的檢查只在 `kind == "reference"` 的分支裡跑，只驗證了『出現在
    某個 ref_table 位置』的表——一張沒有主鍵、也沒被任何表參照的表（真實
    T100 常見，例如純明細／log 表）會完整通過 `plan_load`，然後在
    Task 10 整張表塌成一筆，而 `plan_load` 完全沒有機會攔下來，因為它
    根本不出現在任何一條 reference 裡。
    """
    mapping = {
        "models": [{"source_table": "standalone_no_pk", "target_model": "model.standalone"}],
        "mappings": [],
    }
    with pytest.raises(UnsupportedReference) as exc:
        plan_load(mapping, _reader_with_no_primary_key("standalone_no_pk"), source_id="c3")
    message = str(exc.value)
    assert "standalone_no_pk" in message
    assert "沒有宣告主鍵" in message


def test_composite_pk_table_that_is_never_referenced_is_allowed():
    """複合主鍵完全合法——只有「被參照的表」才不能是複合主鍵。

    `build_external_id` 本來就吃多個 PK 值，一張表為自己的每一列組
    external_id 用複合主鍵完全沒問題。若把『每張表恰好一個 PK』誤當成
    規則，會直接打爆真實語料：case03 的 `ord_deta` 主鍵就是
    `(ord_no, ord_seq)`，且 `ord_deta` 沒有被任何表參照。
    """
    mapping = {
        "models": [{"source_table": "detail_tbl", "target_model": "model.detail"}],
        "mappings": [],
    }
    plans = plan_load(mapping, _reader_with_composite_pk("detail_tbl"), source_id="c3")
    assert [p.source_table for p in plans] == ["detail_tbl"]


def test_reference_column_must_match_the_referenced_tables_actual_primary_key():
    """C3：`ref_column` 必須就是被參照表實際的主鍵欄名，不能只檢查 PK 是
    不是單欄。

    `mapping.json` 的 `ref_column` 可能被人手動改錯（打錯字、複製貼上
    舊欄名……）。若不比對，Task 10 會拿 `row[ref_column]` 的值（根本不是
    PK 值）去組 external_id，指到一筆不存在的記錄，或更糟，剛好撞到另一
    筆真實記錄——這裡真實 PK 是 `real_pk`，mapping 宣告成
    `TOTALLY_WRONG_COL`，必須被擋下。
    """
    mapping = {
        "models": [
            {"source_table": "p", "target_model": "model.parent"},
            {"source_table": "child_tbl", "target_model": "model.child"},
        ],
        "mappings": [
            _reference(
                "child_tbl", "parent_no", "model.child", "parent_id", "p", "TOTALLY_WRONG_COL"
            ),
        ],
    }
    with pytest.raises(UnsupportedReference) as exc:
        plan_load(mapping, _reader_with_mismatched_pk_name("p", "real_pk"), source_id="c3")
    message = str(exc.value)
    assert "p" in message
    assert "real_pk" in message
    assert "TOTALLY_WRONG_COL" in message


def test_field_mapping_source_table_not_in_models_is_rejected():
    """I2：`mapping["mappings"]` 裡的 `source.table` 沒有出現在
    `mapping["models"]` 裡時，必須明確拒絕，不能讓一個從未被檢查過的表名
    直接流進 `columns_by_table[table].append(...)`，變成使用者看不懂的
    `KeyError`。

    `source.py` 的 `_check_table()` 對同樣性質的問題（表名可能被人手動
    編輯過）已經有這個標準；`ref_table` 那一側也已經檢查——這裡補上
    `source.table` 這一側，讓同一份 mapping 的兩個欄位用同一套標準。
    """
    mapping = {
        "models": [{"source_table": "real_tbl", "target_model": "model.real"}],
        "mappings": [
            _identity("ghost_src", "col", "model.real", "field"),
        ],
    }
    with pytest.raises(UnsupportedReference) as exc:
        plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")
    assert "ghost_src" in str(exc.value)


def test_table_plan_target_model_matches_mapping_models():
    """I1：`TablePlan.target_model` 必須是 `mapping["models"]` 宣告的目標
    模型，一字不改。

    修正前這個值完全沒有測試斷言過——mutation 把它改成 `.upper()` 仍然
    全數通過。Task 10 拿這個字串決定 `env[model]`，取錯就是整批資料寫進
    錯的 Odoo 模型。
    """
    plans = plan_load(
        _mapping_with_case03_shape(), _reader_with_single_column_pks(), source_id="c3"
    )
    assert {p.source_table: p.target_model for p in plans} == {
        "ord_deta": "sale.order.line",
        "ord_mast": "sale.order",
        "prod_mast": "product.product",
        "cust_mast": "res.partner",
    }


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
    """同一張表底下的欄位順序必須是確定性的——依「來源欄位名」字典序排序
    （不是依目標欄位名）。

    fixture 刻意讓 source 與 target 的字典序**相反**：`zeta_col` 對到
    target `alpha`、`alpha_col` 對到 target `zeta`。若排序 key 換成
    `target["field"]`（Mi1 的 mutation M42），這裡會得到
    `["zeta_col", "alpha_col"]` 而不是斷言的
    `["alpha_col", "zeta_col"]`——上一輪的 fixture 用 `zeta_col→zeta`／
    `alpha_col→alpha`，兩個 key 排出來剛好一樣，測不出這個差異。
    """
    mapping = {
        "models": [{"source_table": "t", "target_model": "model.t"}],
        "mappings": [
            _identity("t", "zeta_col", "model.t", "alpha"),
            _identity("t", "alpha_col", "model.t", "zeta"),
        ],
    }
    plans = plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")
    columns = [c.source_column for c in plans[0].columns]
    assert columns == ["alpha_col", "zeta_col"]


def test_column_plan_transform_is_a_copy_not_the_same_dict_object_as_the_mapping():
    """I5：`ColumnPlan.transform` 必須是淺拷貝，不能是呼叫端傳入的
    `mapping` 裡那個 dict 物件本身。

    若沒有這條，下游（Task 8）原地修改 `plan.transform` 會回頭改到呼叫端
    保有的 `mapping` 物件——`plan_load` 明明已經回傳過一次，呼叫端卻可能
    在不知情的狀況下被改動了自己手上的 mapping。
    """
    mapping = _mapping_with_case03_shape()
    plans = plan_load(mapping, _reader_with_single_column_pks(), source_id="c3")
    ord_mast = next(p for p in plans if p.source_table == "ord_mast")
    partner = next(c for c in ord_mast.columns if c.source_column == "ord_cust_no")
    original_transform = next(
        fm["transform"]
        for fm in mapping["mappings"]
        if fm["source"]["table"] == "ord_mast" and fm["source"]["column"] == "ord_cust_no"
    )
    assert partner.transform == original_transform
    assert partner.transform is not original_transform


def test_plan_load_returns_table_plan_and_column_plan_instances():
    """回傳型別要真的是任務書指定的 `TablePlan`／`ColumnPlan`，不是巧合同形的
    普通 dict 或 tuple。"""
    plans = plan_load(
        _mapping_with_case03_shape(), _reader_with_single_column_pks(), source_id="c3"
    )
    assert all(isinstance(p, TablePlan) for p in plans)
    assert all(isinstance(c, ColumnPlan) for p in plans for c in p.columns)
