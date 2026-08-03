"""套用 `mapping.json` 裡的四種 transform，把來源值轉成 Odoo `load()` 吃得下的字串。

Odoo 的 `load()` 吃的是**字串矩陣**（`odoo/models.py:1158` 的 docstring：
`:type data: list(list(str))`），所以 `apply_transform` 一律回傳 `str`，
不論輸入的原值是什麼型別。
"""

from __future__ import annotations

from typing import Any

from erp_migrai.pipeline.planner import build_external_id


class TransformError(Exception):
    """轉換失敗。載入階段把它轉成 E-4002，訊息必須點名是哪個表的哪個欄位。"""


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
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return _bool_to_import_str(value)
    return str(value)


def _apply_identity(value: Any) -> str:
    return _value_to_import_str(value)


def _apply_value_map(transform: dict[str, Any], value: Any, *, where: str) -> str:
    """用**字串化後的原值**查 `value_map`。

    語料的真實形狀（`value_map` 的鍵一定是字串，因為 JSON 物件的鍵只能
    是字串）是 `{"Y": true, "N": false}`，原值是 `str`（`'Y'`／`'N'`）；
    但這裡刻意先把原值轉成字串再查表，而不是要求呼叫端保證原值已經是
    字串——來源資料庫裡同一欄位型別不保證跨來源系統一致（例如某些來源
    的旗標欄位是 `int` 的 `0`/`1`），字串化後統一用字串鍵查，避免因為原值
    型別的差異（`1` 對 `"1"`）而在明明語意相同的值上得到「查不到」的
    結果。
    """
    value_map: dict[str, Any] = transform.get("value_map") or {}
    key = _value_to_import_str(value)
    if key in value_map:
        return _value_to_import_str(value_map[key])
    default = transform.get("default")
    # 🔴 必須用 `is None` 而非 `not default`：`default=False` 是完全合法的
    # 規則（「認不得的狀態一律視為停用」），`not False` 會誤判成「沒有
    # default」而錯誤地拋出 TransformError。
    if default is None:
        raise TransformError(
            f"{where}：值 {key!r} 不在 value_map 裡，且沒有設定 default，無法決定要寫入什麼"
        )
    return _value_to_import_str(default)


def _apply_constant(transform: dict[str, Any]) -> str:
    return _value_to_import_str(transform.get("value"))


def _apply_reference(transform: dict[str, Any], value: Any, *, source_id: str) -> str:
    """把原值解析成 `ref_table` 那張來源表的 external_id。

    原值為 `None`：可空外鍵，來源就是 NULL，不是錯誤，是「這筆沒有
    關聯」，回傳空字串（Odoo 匯入把 `<field>/id` 欄位的空字串當「沒有
    關聯」，見 `ir_fields.py:445-448`：`subfield == 'id'` 時先把值餵給
    `_str_to_boolean`，空字串落在 falses 集合，回傳 `False` 讓 Odoo 當成
    「這筆沒有關聯」）。

    這裡必須在呼叫 `build_external_id`／`encode_pk_value` **之前**就攔下
    `None`——那兩個函式蓄意拒絕 `None`（`ValueError`），`None` 不該流進去。
    """
    if value is None:
        return ""
    return build_external_id(source_id, transform["ref_table"], [value])


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
    kind = transform["kind"]
    if kind == "identity":
        return _apply_identity(value)
    if kind == "value_map":
        return _apply_value_map(transform, value, where=where)
    if kind == "constant":
        return _apply_constant(transform)
    if kind == "reference":
        return _apply_reference(transform, value, source_id=source_id)
    # 四種 kind 都在上面窮盡列舉；`TransformKind` 之外的值代表 mapping.json
    # 本身壞掉了（可能是手動編輯出的錯字），不該落進某種「不認得就原樣
    # 回傳」的預設分支悄悄產生一筆看似合法、實則語意不明的資料。
    raise TransformError(f"{where}：不認得的 transform kind {kind!r}")
