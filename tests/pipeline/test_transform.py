"""`pipeline/transform.py` 的行為契約：四種 transform 的套用。

## 布林值的字串表示——已對 Odoo 原始碼核對，不是待實測的猜測

任務書原本把 `str(True) == "True"` 是否為 Odoo 匯入轉換器接受的形式列為
「本機驗不到，須待 Task 12 CI 實測」的暫定值。但這件事查原始碼就能確定：
`odoo/addons/base/models/ir_fields.py:208-236` 的 `_str_to_boolean` 對
`value.lower()` 比對——

    trues  = {"1", "true", "yes", ...}
    falses = {"", "0", "false", "no", ...}

`str(True).lower() == "true"` 落在 `trues`；`str(False).lower() ==
"false"` 落在 `falses`。因此下面的斷言不是「暫定值」，是對照原始碼行為
釘住的硬斷言——往後若有人想改成 `"1"`／`"0"`，必須連這條測試與上面的
docstring 引用一起改，不會悄悄改掉。

## `reference` 回傳空字串的語意——同樣已對原始碼核對

`ir_fields.py:445-448`：`subfield == 'id'`（也就是 `<field>/id` 路徑）
會先把值餵給 `_str_to_boolean`；空字串落在 falses 集合，Odoo 因此把它
當成「這筆沒有關聯」而非錯誤。我們的 external_id 一律以 `migrai.` 開頭，
不會落進 falses、也一定含 `.`，不會被誤判。
"""

from __future__ import annotations

import copy

import pytest

from erp_migrai.pipeline.transform import TransformError, apply_transform

# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def test_identity_none_becomes_empty_string():
    assert apply_transform({"kind": "identity"}, None, source_id="c3", where="t.c") == ""


def test_identity_passes_string_through():
    """CHAR 補空白由 `SourceReader` 層負責，這裡刻意不做 `.strip()`——原值
    前後帶空白時必須原樣保留（見 `_value_to_import_str` docstring）。舊版
    fixture 用的是 `"hello"`（前後沒有空白），strip／不 strip 在那個值上
    答案相同，分辨不出這個決定有沒有被守住（M44：把 `_apply_identity`
    改成呼叫 `.strip()` 一樣全綠）；改成前後帶空白的值才能真正釘住。"""
    assert (
        apply_transform({"kind": "identity"}, "  hello  ", source_id="c3", where="t.c")
        == "  hello  "
    )


def test_identity_int():
    assert apply_transform({"kind": "identity"}, 1000, source_id="c3", where="t.c") == "1000"


def test_identity_float_amount():
    """金額欄位常見的浮點數；`str()` 的預設格式化必須維持人類看得懂的十進位。"""
    assert apply_transform({"kind": "identity"}, 333.33, source_id="c3", where="t.c") == "333.33"


def test_identity_float_large_number_becomes_scientific_notation():
    """`str()` 對很大的浮點數會退化成科學記號——這是刻意記錄下來的既有限制。

    已對 Odoo 17 原始碼核對是否會被吃下，不是留白臆測：
    `odoo/addons/base/models/ir_fields.py:250-258` 的 `_str_to_float`
    （`_str_to_monetary = _str_to_float`，`:260`）是 `float(value)`，
    `float('1e+16')` 成功——Float／Monetary 目標欄位吃得下科學記號。
    但 `ir_fields.py:239-247` 的 `_str_to_integer` 是 `int(value)`，
    `int('1e+16')` 拋 `ValueError`，Odoo 會轉成
    `"'1e+16' does not seem to be an integer for field ..."`——落在
    Integer 目標欄位是大聲失敗，不是靜默塞進一筆錯的資料。更常見的觸發
    形狀：`str(10.0) == '10.0'` 落在 Integer 目標欄位，`int('10.0')`
    一樣 `ValueError`、一樣大聲，不是本任務特有的邊界情況。

    這條測試只是把「現在的行為是什麼」釘住，避免未來
    `_value_to_import_str` 改了實作卻沒人注意到這個邊界跟著變了。
    """
    assert apply_transform({"kind": "identity"}, 1e16, source_id="c3", where="t.c") == "1e+16"


def test_identity_int_large_number_stays_decimal():
    """同樣的大小，`int` 不會退化成科學記號——這是 `int`／`float` 的差異，
    不是 `apply_transform` 額外做了什麼特殊處理。"""
    assert (
        apply_transform({"kind": "identity"}, 10**16, source_id="c3", where="t.c")
        == "10000000000000000"
    )


def test_identity_bool_true():
    """已對 Odoo 原始碼核對：見本檔案開頭的模組 docstring。"""
    assert apply_transform({"kind": "identity"}, True, source_id="c3", where="t.c") == "True"


def test_identity_bool_false():
    """已對 Odoo 原始碼核對：見本檔案開頭的模組 docstring。"""
    assert apply_transform({"kind": "identity"}, False, source_id="c3", where="t.c") == "False"


def test_identity_empty_string_stays_empty_string():
    """空字串本身就是合法的原值（不是 `None`），必須原樣保留，不能被
    誤判成「沒有值」而做了額外處理。"""
    assert apply_transform({"kind": "identity"}, "", source_id="c3", where="t.c") == ""


def test_identity_non_ascii():
    assert (
        apply_transform({"kind": "identity"}, "台南股份有限公司", source_id="c3", where="t.c")
        == "台南股份有限公司"
    )


# --------------------------------------------------------------------------
# value_map
# --------------------------------------------------------------------------


def test_value_map_hit():
    """語料的真實形狀（case01／case03 共三筆一致）：鍵是 `str`、值是
    `bool`。已對 Odoo 原始碼核對，`"False"` 不是暫定值——見模組 docstring。
    """
    t = {"kind": "value_map", "value_map": {"Y": True, "N": False}, "default": True}
    assert apply_transform(t, "N", source_id="c3", where="t.c") == "False"


def test_value_map_hit_other_branch():
    t = {"kind": "value_map", "value_map": {"Y": True, "N": False}, "default": True}
    assert apply_transform(t, "Y", source_id="c3", where="t.c") == "True"


def test_value_map_miss_without_default_raises_naming_the_column():
    """查不到又沒有 default 時必須大聲失敗。

    悄悄塞一個空值進去，會讓 Odoo 收到一筆語意錯誤但格式合法的資料——
    那正是「絕不悄悄產生錯的輸出」要擋的東西。
    """
    t = {"kind": "value_map", "value_map": {"Y": True}, "default": None}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "Z", source_id="c3", where="ord_mast.ord_stus")
    assert "ord_mast.ord_stus" in str(exc.value)
    assert "Z" in str(exc.value)


def test_value_map_miss_with_default_false_is_used_not_treated_as_missing():
    """🔴 `default=False` 是合法規則（「認不得的狀態一律視為停用」），
    不可以被誤判成「沒有 default」而拋錯。

    判斷式必須寫 `if default is None`，不可以寫 `if not default`——
    `not False` 也是 `True`，寫錯的話這條測試會紅：合法的 `default=False`
    會被誤判成「沒有 default」而錯誤地拋出 `TransformError`。
    """
    t = {"kind": "value_map", "value_map": {"Y": True}, "default": False}
    assert apply_transform(t, "Z", source_id="c3", where="t.c") == "False"


def test_value_map_original_value_is_int_looked_up_by_stringified_key():
    """JSON 的 `value_map` 鍵一定是字串，但原值可能是 `int`（例如某些來源
    系統的旗標欄位用 0/1 而非 Y/N）。契約是「用字串化後的原值查」，所以
    `1`（int）必須查得到鍵 `"1"`。"""
    t = {"kind": "value_map", "value_map": {"1": True, "0": False}, "default": None}
    assert apply_transform(t, 1, source_id="c3", where="t.c") == "True"


def test_value_map_empty_dict_falls_back_to_default():
    t = {"kind": "value_map", "value_map": {}, "default": "fallback"}
    assert apply_transform(t, "anything", source_id="c3", where="t.c") == "fallback"


def test_value_map_mapped_value_is_none():
    """`value_map` 裡某個鍵本身對到 `None`（例如刻意把某個離散值對到
    「清空這個欄位」）：查得到、不是「查不到」，回傳空字串而非拋錯。"""
    t = {"kind": "value_map", "value_map": {"X": None}, "default": "d"}
    assert apply_transform(t, "X", source_id="c3", where="t.c") == ""


def test_value_map_none_raises_naming_where():
    """審查修正輪次 1 I2：`mapping.schema.json` 允許 `kind == "value_map"`
    時 `value_map` 為 `null`，但那語意上是「這條規則沒有真的帶對照表」，
    是結構壞掉，不是合法的空表。舊版 `transform.get("value_map") or {}`
    會把它悄悄吸收成空表、每一列都靜默套用 `default`，零錯誤零警告
    （M11／M34 全綠證實沒有測試分辨得出來）；改成大聲失敗。"""
    t = {"kind": "value_map", "value_map": None, "default": "X"}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "anything", source_id="c3", where="ord_mast.ord_stus")
    assert "ord_mast.ord_stus" in str(exc.value)
    # 只斷言 where 的話，這條訊息與「必須是物件」那條可以互換而測試全綠
    # （複驗的 N10）——兩種結構壞法要給使用者不同的診斷，各自釘住關鍵詞。
    assert "None" in str(exc.value)


def test_value_map_non_dict_raises_naming_where():
    """`value_map` 若不是 `dict`（例如手動編輯 mapping.json 打錯型別），
    同樣是結構壞掉，必須大聲失敗而不是讓後面的 `key in value_map` 用
    奇怪的方式失敗（字串的 `in` 不會拋錯，會查到子字串，讓錯誤更難查）。
    """
    t = {"kind": "value_map", "value_map": "not-a-dict", "default": "X"}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "anything", source_id="c3", where="t.c")
    assert "t.c" in str(exc.value)
    assert "必須是物件" in str(exc.value)  # 與「是 None」那條不可互換，見上


def test_value_map_missing_value_map_key_raises_naming_where():
    """缺 `value_map` 這個鍵本身（而不是值為 `None`）：直接索引必須大聲
    失敗，且是點名 `where` 的 `TransformError`，不是裸 `KeyError`。"""
    t = {"kind": "value_map", "default": "X"}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "anything", source_id="c3", where="t.c")
    assert "t.c" in str(exc.value)
    assert "缺少必要的鍵" in str(exc.value)
    assert "value_map" in str(exc.value)


def test_value_map_missing_default_key_raises_naming_where():
    """缺 `default` 這個鍵本身：同上，直接索引必須大聲失敗。

    這裡刻意斷言訊息點名「缺少必要的鍵」，而不是只斷言「有 TransformError
    且提到 where」——單靠後者無法分辨是真的直接索引擋下缺鍵，還是退化成
    `transform.get("default")`（缺鍵時悄悄回傳 `None`，再落進「查不到又
    沒有 default」那個既有分支，恰好也會產生一個提到 where 的
    `TransformError`，訊息不同但兩者都會通過寬鬆的斷言）。"""
    t = {"kind": "value_map", "value_map": {"Y": True}}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "Z", source_id="c3", where="t.c")
    assert "t.c" in str(exc.value)
    assert "缺少必要的鍵" in str(exc.value)
    assert "'default'" in str(exc.value)


def test_value_map_does_not_mutate_the_shared_transform_dict():
    """`transform` 是 `plan_load` 交出來的淺拷貝，巢狀的 `value_map` 仍與
    呼叫端共享同一個 dict 物件；`apply_transform` 絕對不能原地修改它。"""
    value_map = {"Y": True, "N": False}
    t = {"kind": "value_map", "value_map": value_map, "default": None}
    before = copy.deepcopy(t)
    apply_transform(t, "Y", source_id="c3", where="t.c")
    assert t == before
    assert t["value_map"] is value_map


# --------------------------------------------------------------------------
# constant
# --------------------------------------------------------------------------


def test_constant_ignores_original_value():
    t = {"kind": "constant", "value": "台灣"}
    assert apply_transform(t, "這個值應該被忽略", source_id="c3", where="t.c") == "台灣"


def test_constant_value_is_none_raises_instead_of_becoming_empty_string():
    """審查修正輪次 1 I2：🔴 控制者裁決——`kind == "constant"` 而
    `value is None` 是結構壞掉，不是「常數恰好是空字串」。舊版
    `transform.get("value")` 會把它悄悄轉成空字串（M16 全綠證實沒有
    測試分辨得出來）；規則作者若真的想寫常數空字串，必須明確填
    `value: ""`（見下一條測試），`value: null` 代表這個欄位根本沒被填過。
    """
    t = {"kind": "constant", "value": None}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "irrelevant", source_id="c3", where="prod_mast.some_const")
    assert "prod_mast.some_const" in str(exc.value)


def test_constant_value_is_explicit_empty_string_stays_empty_string():
    """對照上一條：真的要常數空字串，必須明確寫 `value: ""`，不是
    `null`——兩者現在不再是同一種輸入。"""
    t = {"kind": "constant", "value": ""}
    assert apply_transform(t, "irrelevant", source_id="c3", where="t.c") == ""


def test_constant_missing_value_key_raises_naming_where():
    """缺 `value` 這個鍵本身：直接索引必須大聲失敗。"""
    t = {"kind": "constant"}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "irrelevant", source_id="c3", where="t.c")
    assert "t.c" in str(exc.value)
    assert "缺少必要的鍵" in str(exc.value)
    assert "value" in str(exc.value)


def test_constant_value_is_false():
    t = {"kind": "constant", "value": False}
    assert apply_transform(t, "irrelevant", source_id="c3", where="t.c") == "False"


def test_constant_value_is_number():
    t = {"kind": "constant", "value": 42}
    assert apply_transform(t, "irrelevant", source_id="c3", where="t.c") == "42"


# --------------------------------------------------------------------------
# reference
# --------------------------------------------------------------------------


def test_reference_builds_external_id():
    t = {"kind": "reference", "ref_table": "cust_mast", "ref_column": "cust_no"}
    got = apply_transform(t, "C001", source_id="case03", where="ord_mast.ord_cust_no")
    assert got == "migrai.case03.cust_mast.C001"


def test_reference_with_null_value_is_empty_not_an_error():
    """可空外鍵：來源就是 NULL 時不是錯誤，是「這筆沒有關聯」。"""
    t = {"kind": "reference", "ref_table": "cust_mast", "ref_column": "cust_no"}
    assert apply_transform(t, None, source_id="c3", where="t.c") == ""


def test_reference_with_empty_string_value_is_not_treated_as_null():
    """空字串不是 `None`：它是「來源值剛好是空字串」這個（詭異但合法的）
    情況，必須照樣走 `build_external_id`、被編碼成該值對應的 external_id，
    不可以被誤判成「NULL 外鍵」而回傳空字串。"""
    t = {"kind": "reference", "ref_table": "cust_mast", "ref_column": "cust_no"}
    got = apply_transform(t, "", source_id="c3", where="t.c")
    assert got != ""
    assert got == "migrai.c3.cust_mast."


def test_reference_missing_ref_table_key_raises_naming_where():
    """審查修正輪次 1 I1：缺 `ref_table` 這個鍵本身，舊版是裸
    `KeyError: 'ref_table'`——不是 `TransformError`，訊息也不含 `where`。
    `mapping.schema.json` 把 `ref_table` 列進 `required`，schema-valid
    的 mapping.json 理論上不會缺這個鍵，但這裡仍要為「呼叫端沒先過
    schema 驗證」的情況負責。"""
    t = {"kind": "reference"}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "C001", source_id="c3", where="ord_mast.ord_cust_no")
    assert "ord_mast.ord_cust_no" in str(exc.value)
    assert "缺少必要的鍵" in str(exc.value)
    assert "ref_table" in str(exc.value)


def test_reference_ref_table_none_raises_naming_where():
    """審查修正輪次 1 I1：🔴 `ref_table: null` 是 schema-valid 的
    mapping.json 形狀（`mapping.schema.json` 把 `ref_table` 標成
    `["string", "null"]`，跨欄位的「reference 必須有 ref_table」規則
    留給 rules_loader，而 rules_loader 只跑在規則 YAML 上、不跑在
    mapping.json 上）。舊版會讓這個值一路流進
    `encode_pk_value`，炸出不點名 `where` 的 `ValueError:
    encode_pk_value 不接受 None`——不是 `TransformError`。"""
    t = {"kind": "reference", "ref_table": None, "ref_column": "cust_no"}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "C001", source_id="c3", where="ord_mast.ord_cust_no")
    assert "ord_mast.ord_cust_no" in str(exc.value)


def test_reference_ref_table_empty_string_raises_instead_of_silently_building_id():
    """審查修正輪次 1 I1：舊版對 `ref_table: ""` 完全靜默——照樣組出
    `migrai.c3..C001` 這種看似合法、實則語意不明的 external_id（`""`
    在 `_apply_reference` 原本沒有任何檢查）。改成大聲失敗。"""
    t = {"kind": "reference", "ref_table": "", "ref_column": "cust_no"}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "C001", source_id="c3", where="ord_mast.ord_cust_no")
    assert "ord_mast.ord_cust_no" in str(exc.value)


# --------------------------------------------------------------------------
# 未知 kind／where 定位字串／enum 完整覆蓋
# --------------------------------------------------------------------------


def test_missing_kind_key_raises_naming_where():
    """審查修正輪次 1 I1：缺 `kind` 這個鍵本身，舊版是裸
    `KeyError: 'kind'`——不是 `TransformError`，訊息也不含 `where`。"""
    t: dict[str, object] = {}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "x", source_id="c3", where="prod_mast.no_kind")
    assert "prod_mast.no_kind" in str(exc.value)
    assert "缺少必要的鍵" in str(exc.value)
    assert "kind" in str(exc.value)


def test_transform_error_base_class_is_exception_not_value_error():
    """M38：若 `TransformError` 改成繼承 `ValueError`，Task 10（或任何
    呼叫端）既有的 `except ValueError` 會搶先吃掉它，讓一次真正的
    transform 失敗被誤判成別的錯誤代碼。這裡把基底類別釘住為
    `Exception`，不是 `ValueError` 的子類別。"""
    assert TransformError.__bases__ == (Exception,)


def test_unknown_kind_raises_loudly_naming_where_and_the_kind():
    """不認得的 `kind` 必須大聲失敗，不可以掉進「原樣回傳」的預設分支——
    那會讓一筆資料以看似合法、實則語意不明的形式流進 Odoo。"""
    t = {"kind": "no_such_kind"}
    with pytest.raises(TransformError) as exc:
        apply_transform(t, "x", source_id="c3", where="prod_mast.weird_field")
    assert "prod_mast.weird_field" in str(exc.value)
    assert "no_such_kind" in str(exc.value)


def test_every_transform_kind_is_handled():
    """四種 kind 都必須有實作，不可以有一種掉進「不認得就原樣回傳」的預設分支。

    對 enum 斷言完整值集合（原則 5.4）：只測四種各一次，抓不到「有人加了
    第五種但忘了在這裡實作」。
    """
    from erp_migrai.mapping.models import TransformKind

    for kind in TransformKind:
        t = {
            "kind": kind.value,
            "value_map": {"x": "y"},
            "default": "d",
            "value": "v",
            "ref_table": "t",
            "ref_column": "c",
        }
        apply_transform(t, "x", source_id="c3", where="t.c")  # 不得拋 KeyError／NotImplementedError
