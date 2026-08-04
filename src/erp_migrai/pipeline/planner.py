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

# 多個 PK 值之間的分隔字元。抽成常數而不是在 build_external_id 裡寫死
# 字面值 "-"，是為了讓「分隔字元必須不在 _UNRESERVED_BYTES 裡」這條
# 不變式能被獨立測試釘住——見下面的 assert 與
# test_separator_is_not_in_the_unreserved_byte_set。
_SEPARATOR = "-"
assert ord(_SEPARATOR) not in _UNRESERVED_BYTES, (
    "分隔字元必須不在 unreserved 集合裡，否則會被誤認為值本身的一部分"
)
# `%` 是 encode_pk_value 逐位元組跳脫序列自己的前導字元（`%XX`）。分隔字元
# 若也是 `%`，組出來的字串會沒辦法唯一還原：無法分辨一個 `%` 是「這裡是
# 分隔字元」還是「這裡是某個位元組被跳脫成 %XX 的開頭」。這條不變式跟
# 「不在 unreserved 集合裡」是兩件事——`%` 本身也不在 unreserved 集合裡
# （見 `_UNRESERVED_BYTES` 的定義），上面那條 assert 擋不住這個。
assert _SEPARATOR != "%", "分隔字元不能是 %，那是跳脫序列自己的前導字元"


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

    `None` 不是合法的 PK 值：NULL 外鍵在真實 T100 很常見（客戶未指定、
    上層單號留空），若把 `None` 編碼成字面上的字串 `"None"`，一筆
    「PK 是 NULL」與另一筆「PK 剛好是字串 'None'」的來源資料會被悄悄
    組成同一個 external_id——這正是「絕不悄悄產生錯的輸出」要擋的形狀。
    這裡直接拒絕，NULL 外鍵由呼叫端（Task 10）在餵值之前判斷並跳過該格，
    不是這個函式該吸收的責任。
    """
    if value is None:
        raise ValueError("encode_pk_value 不接受 None：NULL 外鍵應由呼叫端在餵值之前判斷並跳過該格")
    return "".join(
        chr(byte) if byte in _UNRESERVED_BYTES else f"%{byte:02X}"
        for byte in str(value).encode("utf-8")
    )


def build_external_id(source_id: str, table: str, pk_values: Sequence[Any]) -> str:
    """組出 `migrai.<source_id>.<table>.<編碼後PK>`。

    `source_id`（呼叫端傳入、代表是哪一個來源系統）與 `table`（來自 DDL）
    跟 PK 值一樣過 `encode_pk_value` 同一套編碼，理由與 PK 值的編碼同源：
    三段都不保證只含 `[A-Za-z0-9_]`，若不編碼，分隔字元（`.`）在其中一段
    裡的位置差異可以組出完全相同的字串——

        (source_id="a.b", table="c")   → 不編碼會變成 "migrai.a.b.c.X"
        (source_id="a",   table="b.c") → 不編碼也會變成 "migrai.a.b.c.X"

    嚴格來說，只要 PK 值本身已編碼（`enc(pk)` 保證不含 `.`），
    `source_id`／`table` 兩段之中**任一個**有編碼就足以杜絕碰撞：
    字串可以從右邊唯一地切分出 `enc(pk)`、再切出 `enc(table)`
    （兩者都不含 `.`），剩下的就是 `source_id` 的編碼結果，不會有第二種
    切法讓兩筆不同輸入湊出同一個字串。**兩段都編碼是雙保險**（保留、
    對稱、不必記住「兩個裡面選哪一個可以不編碼」），但推不出「兩段都
    必須編碼」——上一版報告寫的推理只對「兩者都不編碼」這個反例成立，
    這裡把理由更正。

    對真實輸入（`case03`、`cust_mast` 這類只含 `[A-Za-z0-9_]` 的名字）
    這一步是零成本：編碼後原樣不變，`build_external_id("case03",
    "cust_mast", ["C001"]) == "migrai.case03.cust_mast.C001"` 依然成立。

    多個 PK 值（複合主鍵）之間用 `_SEPARATOR` 連接——同樣的道理，值內的
    分隔字元已經在 `encode_pk_value` 那一步被編碼掉，這裡的 `_SEPARATOR`
    才是安全的、純粹當分隔字元用的字面值。

    `pk_values` 不可以是空序列：沒有任何 PK 值就組不出可以辨識「這是哪
    一筆記錄」的 external_id（退化成結尾留一個空字串區段，所有列都會撞
    成同一個字串）。呼叫端該在更早的地方（`plan_load` 對「表沒有宣告
    主鍵」的檢查）就大聲拒絕，不該讓這裡安靜地生出一個沒有意義的值。
    """
    if not pk_values:
        raise ValueError(
            "build_external_id 不接受空的 pk_values：沒有 PK 值組不出可辨識的 external_id"
        )
    encoded_pk = _SEPARATOR.join(encode_pk_value(value) for value in pk_values)
    return f"migrai.{encode_pk_value(source_id)}.{encode_pk_value(table)}.{encoded_pk}"


def build_external_id_module_prefix(source_id: str) -> str:
    """`build_external_id` 產出字串裡 `migrai.` 之後、`table` 之前的那一段
    （即只到 `source_id` 為止的前綴，`migrai.<編碼後source_id>.`）。

    最終整分支審查 Critical C1：Task 11 有幾處只需要按 `source_id` 過濾
    （不管 `table`），原本各自用裸 f-string `f"{_MIGRAI_MODULE}.{source_id}."`
    組這段前綴，沒有過 `encode_pk_value`——`build_external_id` 寫入時
    `source_id` 是編碼過的，這裡比對時卻拿未編碼的原始值去 `startswith`，
    兩者只在 `source_id` 剛好只含 `[A-Za-z0-9_]` 時碰巧相等（例如
    `case03`），一旦 `source_id` 含 `-`／`.` 等字元（例如 CI 整合測試用的
    `task12-full`）就永遠比對不上，`verify` 會把「找不到任何一筆」誤判成
    「一筆都沒載入成功」。單一事實來源：這裡跟 `build_external_id` 用
    同一顆 `encode_pk_value`，往後編碼規則變動只需要改一個地方。
    """
    return f"migrai.{encode_pk_value(source_id)}."


def build_external_id_prefix(source_id: str, table: str) -> str:
    """`build_external_id` 產出字串裡 `migrai.` 之後、PK 之前的那一段
    （`<編碼後source_id>.<編碼後table>.`，**不含** `migrai.` 這段模組
    前綴）。

    刻意不含 `migrai.`：呼叫端是 `verify.resolve_target_counts` 與
    `cli._target_line_amount`，兩者比對的對象都是 Odoo `ir.model.data`
    的 `name` 欄位——Odoo 把 external id 拆成 `module`／`name` 兩欄分開
    存放，`name` 欄位本身就不含 module 前綴（查詢時已經用
    `ir_model_data_domain(module=...)` 篩過 module），所以這裡組出來的
    前綴也不該含 `migrai.`，否則永遠比對不上（見
    `build_external_id_module_prefix`，那個函式才含 `migrai.`，給比對
    對象是「完整 external_id 字串」的呼叫端用，例如
    `find_mismatched_external_ids` 讀的 `migration_log.jsonl`）。

    理由同 `build_external_id_module_prefix`：組這段前綴的地方原本各自
    用裸 f-string，沒有過 `encode_pk_value`，跟 `build_external_id` 寫入
    時的編碼對不上。這裡直接呼叫 `encode_pk_value`，確保「組前綴」永遠
    跟「組完整 external_id」用同一套編碼，不會再各自漂移。
    """
    return f"{encode_pk_value(source_id)}.{encode_pk_value(table)}."


@dataclass(frozen=True)
class ColumnPlan:
    """一個來源欄位要怎麼寫進 Odoo。

    `frozen=True` 只鎖住這個 dataclass 自己的欄位不能重新賦值
    （`plan.transform = ...` 會拋 `FrozenInstanceError`），鎖不住
    `transform` 這個 dict 欄位本身的內容——`dict` 是可變物件，
    `plan.transform["kind"] = "x"` 這種原地修改依然合法，也因此
    `ColumnPlan` 儘管標了 `frozen=True` 仍然是不可雜湊的
    （`hash(ColumnPlan(...))` 會拋 `TypeError`）。`plan_load` 建構時會對
    `transform` 做一次淺拷貝（`dict(field_mapping["transform"])`），切斷
    與呼叫端傳入的 `mapping` 共享同一個 dict 物件，但拷貝之後的這份
    `transform` 本身仍然不是不可變的——下游（Task 8）不得原地修改它。
    """

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
    """來源表或 reference 宣告本身有問題，MVP 未支援，包括：

    - `mapping["mappings"]` 的 `source.table` 沒有出現在 `mapping["models"]`
      裡（I2：不知道要把這個欄位寫進哪個 Odoo 模型）。
    - `ref_table` 沒有出現在 `mapping["models"]` 裡（I2：無法解析外鍵）。
    - 計畫裡的表（不論是否被參照）沒有宣告主鍵（C2：組不出可辨識的
      external_id）。
    - 被參照的表是複合主鍵（C1：用部分鍵湊合會安靜地指到錯誤的記錄）。
    - reference 宣告的 `ref_column` 不是被參照表實際的主鍵欄位（C3：
      餵給 `build_external_id` 的值根本不是 PK 值）。

    載入階段把它轉成 E-4001。
    """


def _topological_order(edges: dict[str, set[str]]) -> list[str]:
    """Kahn 演算法：每一輪把「所有還沒被排進去、且剩餘依賴已清空」的節點
    一次取出、依表名字典序排序後整批放進結果，藉此讓「同一層」的節點有
    確定性的順序。

    `edges[table]` 是 `table` 依賴的（必須排在它之前的）表集合。若還有
    節點剩下、但沒有任何一個節點的剩餘依賴是空的，代表這些節點的依賴
    牽扯到至少一個環——但**不代表每一個被點名的表本身都在環上**：
    一張表只要依賴著環上的某張表（即使它自己不在環上），它的剩餘依賴
    也永遠清不空，會被一起列出來（例如 `z_tbl → a_tbl`，`a_tbl` 在
    `a→b→c→a` 的環上、`z_tbl` 不在，但兩者都會出現在錯誤訊息裡）。
    訊息措辭因此只承諾「這些表因為 reference 相依而無法排序，其中至少
    有一個環」，不承諾「以下每一張表都在環上」——MVP 不做強連通分量分析
    來精確標出環的邊界，那是投機的額外複雜度，這裡的錯誤訊息已足以讓
    人回頭去查 `mapping.json` 的 reference 宣告。
    """
    remaining = {table: set(deps) for table, deps in edges.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(table for table, deps in remaining.items() if not deps)
        if not ready:
            cycle = sorted(remaining)
            raise CyclicReference(
                "以下的表因為 reference 相依而無法排序（其中至少有一個環）：" + "、".join(cycle)
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

    `mapping["models"]` 裡 `source_table` 重複時，後面出現的會靜默覆蓋
    前面的（`dict` comprehension 的一般行為）；schema 沒有對這件事做
    uniqueness 約束，這裡假設呼叫端（`migrai map`）產生的 `mapping.json`
    不會有重複，不在這個函式裡另外檢查。

    每張表底下的 `ColumnPlan` 依來源欄位名字典序排序，同樣是為了
    確定性——`mapping["mappings"]` 這份 dict 的原始順序不是這裡承諾的
    契約。

    `source_id` 目前只用來組 external_id（`build_external_id`），不會
    出現在 `mapping.json` 或 `inventory.json` 裡——它是 Task 10 的 CLI
    呼叫端才知道的「這次載入是哪一個來源系統」。

    以下問題會在拓撲排序**之前**就攔下來（用不到排序結果），全部丟
    `UnsupportedReference`：

    - `mapping["mappings"]` 裡的 `source.table` 不在 `mapping["models"]`
      裡：不知道要把這個欄位寫進哪個 Odoo 模型。
    - `ref_table` 不在 `mapping["models"]` 裡：沒有對應到任何 Odoo
      模型，無法解析外鍵。
    - **計畫裡的每一張表**（不論是否被參照）的 PK（由
      `reader.primary_key()` 查得）長度是 0：沒有宣告主鍵，組不出可
      辨識「這是哪一筆記錄」的 external_id。注意這條規則是「至少 1
      個」，不是「恰好 1 個」——複合主鍵完全合法（`build_external_id`
      本來就吃多個 PK 值），例如 `ord_deta` 的主鍵是
      `(ord_no, ord_seq)`。
    - **被參照的表**（出現在某個 `ref_table` 位置）的 PK 長度 > 1：
      複合主鍵，MVP 未支援用複合鍵組外鍵的 external_id（用部分鍵湊合
      會安靜地指到錯誤的記錄）——參照方的那一格只帶得動一個欄位的值，
      湊不出複合鍵。
    - **被參照的表**宣告的 `ref_column` 不等於該表實際的（單欄）主鍵：
      `ref_column` 來自 `mapping.json`，可能被人手動改錯；若不比對，
      Task 10 會拿一個根本不是 PK 的欄位值去組 external_id，指到一筆
      不存在或錯誤的記錄。
    """
    table_to_model = {model["source_table"]: model["target_model"] for model in mapping["models"]}
    field_mappings: list[dict[str, Any]] = mapping["mappings"]

    columns_by_table: dict[str, list[dict[str, Any]]] = {table: [] for table in table_to_model}
    edges: dict[str, set[str]] = {table: set() for table in table_to_model}
    # (來源表, 來源欄位, 被參照表, 宣告的 ref_column) ——蒐集起來，
    # 讓下面對「被參照表」的驗證只需要對每個 distinct 的被參照表做一次
    # （見 Mi7：避免同一張被參照表因為被 N 條 reference 參照而被查詢
    # N 次 PK）。
    reference_occurrences: list[tuple[str, str, str, str]] = []

    for field_mapping in field_mappings:
        table = field_mapping["source"]["table"]
        if table not in columns_by_table:
            raise UnsupportedReference(
                f"{table!r} 不在 mapping['models'] 裡，不知道要把這個欄位寫進哪個 Odoo 模型"
            )
        columns_by_table[table].append(field_mapping)

        transform = field_mapping["transform"]
        if transform["kind"] != "reference":
            continue

        ref_table = transform["ref_table"]
        if ref_table not in table_to_model:
            raise UnsupportedReference(
                f"{table!r} 參照的來源表 {ref_table!r} 沒有對應到任何 Odoo 模型，無法解析外鍵"
            )
        edges[table].add(ref_table)
        reference_occurrences.append(
            (table, field_mapping["source"]["column"], ref_table, transform["ref_column"])
        )

    pk_cache: dict[str, list[str]] = {}

    def _cached_primary_key(table: str) -> list[str]:
        if table not in pk_cache:
            pk_cache[table] = reader.primary_key(table)
        return pk_cache[table]

    # 計畫裡的每一張表都要能組出可辨識的 external_id（C2）：規則是
    # 「至少 1 個 PK 欄位」，不是「恰好 1 個」——複合主鍵在這裡完全合法。
    # 依表名排序只是讓多張表同時違規時，錯誤訊息點名的順序是穩定的。
    for table in sorted(table_to_model):
        if not _cached_primary_key(table):
            raise UnsupportedReference(f"{table!r} 沒有宣告主鍵，無法組出可辨識的 external_id")

    # 被參照的表另外多兩條限制：只能是單欄主鍵（C1），且 reference 宣告
    # 的 ref_column 必須就是那個主鍵欄位（C3）。上面那一輪已經確保每張
    # 表（因此也包含每張被參照表）的 PK 長度不是 0，這裡分成兩個分支仍然
    # 各自給出對應的訊息，不依賴上面那一輪一定先跑過——避免日後有人調整
    # 檢查順序時，「沒有宣告主鍵」的表被套上「是複合主鍵」這種錯誤診斷。
    referenced_tables = sorted({ref_table for _, _, ref_table, _ in reference_occurrences})
    for ref_table in referenced_tables:
        ref_pk = _cached_primary_key(ref_table)
        if not ref_pk:
            raise UnsupportedReference(
                f"被參照的來源表 {ref_table!r} 沒有宣告主鍵，無法組出可辨識的 external_id"
            )
        if len(ref_pk) > 1:
            raise UnsupportedReference(
                f"被參照的來源表 {ref_table!r} 是複合主鍵，MVP 未支援這種外鍵形狀"
            )

    for table, source_column, ref_table, ref_column in reference_occurrences:
        expected = pk_cache[ref_table][0]
        if ref_column != expected:
            raise UnsupportedReference(
                f"{table!r}.{source_column!r} 參照 {ref_table!r}.{ref_column!r}，"
                f"但 {ref_table!r} 的主鍵是 {expected!r}——非主鍵外鍵 MVP 未支援"
            )

    order = _topological_order(edges)

    plans = []
    for table in order:
        columns = tuple(
            ColumnPlan(
                source_column=field_mapping["source"]["column"],
                odoo_field=_odoo_field(field_mapping),
                # 淺拷貝：切斷與呼叫端傳入的 mapping 共享同一個 dict
                # 物件，避免下游原地修改 transform 時回頭改到呼叫端的
                # mapping（見 ColumnPlan docstring）。
                transform=dict(field_mapping["transform"]),
            )
            for field_mapping in sorted(
                columns_by_table[table], key=lambda field_mapping: field_mapping["source"]["column"]
            )
        )
        plans.append(
            TablePlan(source_table=table, target_model=table_to_model[table], columns=columns)
        )
    return plans
