"""套用 `mapping.json` 裡的四種 transform，把來源值轉成 Odoo `load()` 吃得下的字串。

Odoo 的 `load()` 吃的是**字串矩陣**（`odoo/models.py:1158` 的 docstring：
`:type data: list(list(str))`），所以 `apply_transform` 一律回傳 `str`，
不論輸入的原值是什麼型別。
"""

from __future__ import annotations

from typing import Any

from erp_migrai.pipeline.planner import build_external_id


class TransformError(Exception):
    """轉換失敗。載入階段把它轉成 E-4002，訊息必須點名是哪個表的哪個欄位。

    刻意直接繼承 `Exception`、不繼承 `ValueError`（見
    `test_transform_error_base_class_is_exception_not_value_error`）：
    Task 10 用 `except TransformError` 攔截並轉成 E-4002，若哪天有人
    改成讓它繼承 `ValueError`，呼叫端任何既有的 `except ValueError` 都會
    搶先吃掉它，讓它悄悄變成別的錯誤代碼。
    """


def _require_key(transform: dict[str, Any], key: str, *, where: str) -> Any:
    """直接索引 `transform[key]`，缺鍵時轉成點名 `where` 的 `TransformError`。

    `mapping.schema.json` 的 `transform` 物件把 `kind`／`value_map`／
    `default`／`value`／`ref_table`／`ref_column` 六個鍵全部列進
    `required`（值可以是 `null`，但鍵本身必須在）——schema-valid 的
    `mapping.json` 理論上不會缺鍵。但 `apply_transform` 收到的只是一份
    `dict[str, Any]`，不保證呼叫端一定先過 schema 驗證；缺鍵在 Python
    裡就是裸 `KeyError`。

    直接索引（而不是 `.get()` 悄悄回傳 `None`）才符合 Task 7 Mi5 定下的
    規矩：缺鍵是結構性錯誤，必須大聲失敗，不能被吸收成合法的「這個欄位
    剛好沒填」。但裸 `KeyError` 不點名是哪個表的哪個欄位、也不是
    `TransformError`，違反這個模組唯一的錯誤契約——這個小函式把「直接
    索引」與「錯誤必須是點名 `where` 的 `TransformError`」兩件事同時
    做到，讓 `kind`／`ref_table`／`value_map`／`default`／`value` 五處
    存取共用同一套行為。
    """
    try:
        return transform[key]
    except KeyError:
        raise TransformError(f"{where}：transform 缺少必要的鍵 {key!r}") from None


def _bool_to_import_str(value: bool) -> str:
    """把 Python 布林值轉成 Odoo 匯入轉換器接受的字串形式。

    已對 Odoo 17 原始碼核對，不是待實測的猜測：
    `odoo/addons/base/models/ir_fields.py:208-236` 的 `_str_to_boolean`
    對 `value.lower()` 比對——

        trues  = {"1", "true", "yes", ...翻譯...}
        falses = {"", "0", "false", "no", ...翻譯...}

    `str(True)` 是 `"True"`，`.lower()` 後是 `"true"`，落在 `trues` 集合；
    `str(False)` 是 `"False"`，`.lower()` 後是 `"false"`，落在 `falses`
    集合。因此 `str(value)` 本身就是合法的匯入格式，這個函式只是把「布林
    轉字串」這個決定集中在一處，方便日後若要換成 `"1"`／`"0"` 這類其他
    合法形式時只改一個地方，不必逐一翻找呼叫點。
    """
    return str(value)


def _value_to_import_str(value: Any) -> str:
    """把任意值轉成 Odoo 匯入格式的字串，`None` 一律轉成空字串。

    `identity`／`value_map`／`constant` 三種 kind 的「原值轉字串」步驟
    共用這個函式；`reference` 走的是 external_id 編碼，不經過這裡。

    布林值繞去 `_bool_to_import_str`——`bool` 是 `int` 的子類別，若讓它
    落進最後的 `str(value)` 分支結果其實一樣（`str(True) == "True"`），
    但獨立分支讓「布林值要轉成哪種字串」這個決定有一個明確、可單獨修改
    的落點，不必依賴「剛好與 `str()` 的預設行為一致」這個巧合。

    刻意**不對字串值做 `.strip()`**：Informix 的 `CHAR(n)` 是空白補齊的
    （`CHAR(10)` 存 `'C001'`，讀回來是 `'C001      '`），要不要去掉補齊
    空白是 `SourceReader` 那一層的責任，不該在這裡悄悄竄改資料——這裡若
    做了 `.strip()`，會連值本身刻意帶有前後空白的合法資料一起吃掉。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return _bool_to_import_str(value)
    return str(value)


def _apply_value_map(transform: dict[str, Any], value: Any, *, where: str) -> str:
    """用**字串化後的原值**查 `value_map`。

    語料的真實形狀（`value_map` 的鍵一定是字串，因為 JSON 物件的鍵只能
    是字串）是 `{"Y": true, "N": false}`，原值是 `str`（`'Y'`／`'N'`）；
    但這裡刻意先把原值轉成字串再查表，而不是要求呼叫端保證原值已經是
    字串——來源資料庫裡同一欄位型別不保證跨來源系統一致（例如某些來源
    的旗標欄位是 `int` 的 `0`/`1`），字串化後統一用字串鍵查，避免因為原值
    型別的差異（`1` 對 `"1"`）而在明明語意相同的值上得到「查不到」的
    結果。

    `value_map` 本身為 `None` 或不是 `dict`：這是結構壞掉的 transform
    （`mapping.schema.json` 允許 `kind == "value_map"` 時 `value_map` 為
    `null`，那語意上等於「這條規則沒有真的帶對照表」，不是合法的空表）
    ——必須大聲失敗，不能被 `or {}` 這種寫法悄悄吸收成「這張對照表恰好
    是空的」。空表是合法輸入（見 `test_value_map_empty_dict_falls_back_
    to_default`），必須用真正的 `{}` 表達，兩者不能混為一談。
    """
    value_map = _require_key(transform, "value_map", where=where)
    if value_map is None:
        raise TransformError(
            f"{where}：kind 是 value_map 但 value_map 是 None，無法查表"
            "（若規則本意是空對照表，請填 {} 而不是 null）"
        )
    if not isinstance(value_map, dict):
        raise TransformError(f"{where}：value_map 必須是物件，實際是 {value_map!r}")
    key = _value_to_import_str(value)
    if key in value_map:
        # `value_map[key]` 本身是 `None`：這是「查得到、對到清空」（見
        # `test_value_map_mapped_value_is_none`），不是「查不到」。對
        # Boolean 目標欄位而言，空字串與 `"False"` 在 Odoo 的
        # `_str_to_boolean` 裡都落在 falses 集合，兩者等價；但目標欄位
        # 若不是 Boolean，「`null` = 清空這一格」與「`null` = 布林假」的
        # 差異就會浮現（前者是空字串、後者理論上該有個明確的假值字面
        # 量）。這裡統一回傳空字串，把「填 null 代表清空」這個決定留給
        # 規則作者在 `value_map` 裡承擔。
        return _value_to_import_str(value_map[key])
    default = _require_key(transform, "default", where=where)
    # 🔴 必須用 `is None` 而非 `not default`：`default=False` 是完全合法的
    # 規則（「認不得的狀態一律視為停用」），`not False` 會誤判成「沒有
    # default」而錯誤地拋出 TransformError。
    if default is None:
        raise TransformError(
            f"{where}：值 {key!r} 不在 value_map 裡，且沒有設定 default，無法決定要寫入什麼"
        )
    return _value_to_import_str(default)


def _apply_constant(transform: dict[str, Any], *, where: str) -> str:
    """回傳固定值，忽略原值。

    🔴 控制者裁決：`kind == "constant"` 而 `value is None` 時**拋
    `TransformError`，不回傳空字串**。理由：規則作者若真的想寫一個固定
    的空字串常數，應該明確填 `value: ""`；`value: null` 配上
    `kind: constant` 代表這條規則的 `value` 根本沒被填過，是結構壞掉，
    與「`reference` 沒有 `ref_table`」同一類問題——不該讓 `.get()` 幫忙
    決定成「大家都當作空字串」，那等於這個決定沒有人真正做過。
    """
    value = _require_key(transform, "value", where=where)
    if value is None:
        raise TransformError(
            f'{where}：kind 是 constant 但 value 是 None，若要寫入空字串請明確設定 value 為 ""'
        )
    return _value_to_import_str(value)


def _apply_reference(transform: dict[str, Any], value: Any, *, source_id: str, where: str) -> str:
    """把原值解析成 `ref_table` 那張來源表的 external_id。

    原值為 `None`：可空外鍵，來源就是 NULL，不是錯誤，是「這筆沒有
    關聯」，回傳空字串（Odoo 匯入把 `<field>/id` 欄位的空字串當「沒有
    關聯」，見 `ir_fields.py:445-448`：`subfield == 'id'` 時先把值餵給
    `_str_to_boolean`，空字串落在 falses 集合，回傳 `False` 讓 Odoo 當成
    「這筆沒有關聯」）。

    這裡必須在呼叫 `build_external_id`／`encode_pk_value` **之前**就攔下
    `None`——那兩個函式蓄意拒絕 `None`（`ValueError`），`None` 不該流進去。

    `ref_table` 缺席／為 `None`／為空字串：都是結構壞掉的 transform，統一
    拋 `TransformError`。`mapping.schema.json` 把 `ref_table` 標成
    `["string", "null"]`、且不做跨欄位的條件式必填（留給 rules_loader／
    plan_load 在載入時檢查），所以 `{"kind": "reference", "ref_table":
    null}` 是一份 schema-valid 的 `mapping.json` 形狀；若不在這裡擋，
    `transform["ref_table"]` 缺席會是裸 `KeyError`，`ref_table=None` 會讓
    `encode_pk_value` 炸出不點名 `where` 的 `ValueError`，`ref_table=""`
    則會靜默組出 `migrai.<source_id>..<pk>` 這種看似合法、實則語意不明
    的 external_id。

    ⚠️ 這是縱深防禦的第二道，不是唯一一道：`plan_load`
    （`planner.py:290-294`）已經在建構 `ColumnPlan` 之前，對每一筆
    `reference` transform 檢查過 `ref_table` 是否為 `None`／是否落在
    `mapping["models"]` 宣告的表清單裡，不合格會拋 `UnsupportedReference`
    擋在更早的地方。所以 Task 10 從 `plan_load` 拿到的
    `ColumnPlan.transform` 理論上不會踩到這裡的檢查——但 `apply_transform`
    本身的契約（`transform` 是任意 `dict[str, Any]`）並不保證呼叫端一定
    先過 `plan_load`，這一層仍然要為「結構壞掉的輸入」負責，不能只靠
    上游守住。
    """
    # 結構檢查刻意排在 `value is None` 之前：若某個 reference 欄位整欄都是
    # NULL，把檢查放在後面會讓壞掉的 `ref_table` 永遠不浮現——那一欄每一列
    # 都提前 return，結構問題要等到有人填了值才爆，而那可能是好幾個月後的
    # 另一批資料。結構是否合法與這一列的值是什麼無關，先驗結構。
    ref_table = _require_key(transform, "ref_table", where=where)
    if not ref_table:
        raise TransformError(
            f"{where}：reference 的 ref_table 是 {ref_table!r}，無法組出 external_id"
        )
    if value is None:
        return ""
    return build_external_id(source_id, ref_table, [value])


def apply_transform(
    transform: dict[str, Any],
    value: Any,
    *,
    source_id: str,
    where: str,
) -> str:
    """套用一種 transform，回傳可餵給 Odoo `load()` 的字串。

    `where` 是給錯誤訊息用的定位字串，例如 `ord_mast.ord_cust_no`。

    `transform` 是 `plan_load` 交出來的淺拷貝——這個函式絕對不原地修改它
    （包括巢狀的 `value_map`），因為呼叫端與其他表的 `ColumnPlan` 仍可能
    共享同一份巢狀物件。
    """
    kind = _require_key(transform, "kind", where=where)
    if kind == "identity":
        # 原本有一個 `_apply_identity` 轉包函式，只是 `_value_to_import_str`
        # 的一行轉呼叫、沒有自己的實質內容（其他三個 `_apply_*` 都有各自
        # 的規則要處理），已拿掉，這裡直接呼叫更誠實。
        return _value_to_import_str(value)
    if kind == "value_map":
        return _apply_value_map(transform, value, where=where)
    if kind == "constant":
        return _apply_constant(transform, where=where)
    if kind == "reference":
        return _apply_reference(transform, value, source_id=source_id, where=where)
    # 四種 kind 都在上面窮盡列舉；`TransformKind` 之外的值代表 mapping.json
    # 本身壞掉了（可能是手動編輯出的錯字），不該落進某種「不認得就原樣
    # 回傳」的預設分支悄悄產生一筆看似合法、實則語意不明的資料。
    raise TransformError(f"{where}：不認得的 transform kind {kind!r}")
