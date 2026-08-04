"""驗證階段的比對邏輯：純函式，只吃「查詢結果」，不吃「連線」。

設計 spec §2.1：`odoo_client.py` 是整個 package 裡唯一知道網路存在的檔案；
這個模組拿到的一律是**已經查好的數字**（`int`／`Decimal`）或**已經讀好的
資料列**（`Iterable[Mapping]`），連線、RPC、SQL 一律留在 `cli.py`——比
`loader.py` 更嚴格：連「碰 client 的 helper」都不准出現在這個檔案。

## 🔴 float 地雷：來源與目標的金額都必須用 `Decimal(str(value))`

SQLite 的 `DECIMAL(15,4)` 讀回來是 Python `float`；Odoo JSON-RPC 回應的
數值欄位（含 `price_subtotal`）在 JSON 裡也是浮點數，`json.loads` 讀回來
同樣是 Python `float`。`Decimal(0.1)` 不是 `Decimal('0.1')`，而是二進位
浮點數的精確值 `0.1000000000000000055511151231257827021181583404541015625`
——這個坑很安靜：小數字看起來都對，累加幾百筆之後才開始差，而那時候
看起來會像是 Odoo 那邊算錯了。`sum_source_amount`／`sum_target_amount`
因此永遠透過 `Decimal(str(value))` 建構，不接受呼叫端直接餵 `Decimal`
或原始 `float` 進最終比對。

## 為什麼比對 `price_subtotal` 而不是 `amount_total`

已查證 `addons/sale/models/sale_order_line.py:637-639`：
`'price_subtotal': amount_untaxed`、`'price_total': amount_untaxed +
amount_tax`——`price_subtotal` 是未稅，`sale.order.amount_total` 是含稅。
拿來源的未稅小計去比 Odoo 的含稅總額，會因為 Odoo 套了預設稅而產生一個
**假的** `E-5002`，這正是「絕不回報一個不存在的問題」要擋的形狀。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from erp_migrai.events import E_5001, E_5002, ErrorCode
from erp_migrai.pipeline.planner import build_external_id_prefix

# --------------------------------------------------------------------------
# Check：一條比對結果
# --------------------------------------------------------------------------

# 兩個 kind、兩個 status，意義互斥且窮盡（precedent：models.ACTIONS）。
KINDS = frozenset({"row_count", "amount_subtotal"})
STATUSES = frozenset({"pass", "fail"})

# 金額比對的容差：Odoo 預設幣別（TWD／USD）最小單位皆為分，0.01 是貨幣捨入
# 誤差的合理容差，不是拍腦袋（設計 spec §3.6：「容差為 Odoo 的幣別
# rounding（0.01）」）。原本住在 `cli.py`，審查修正輪次 1 Important I4：
# 這個常數沒有任何測試守著——搬過來跟 `assemble_checks` 的預設值放在一起，
# 一條 `assert VERIFICATION_ROUNDING == Decimal("0.01")` 就直接釘死。
VERIFICATION_ROUNDING = Decimal("0.01")

# `_target_line_amount`（`cli.py`）讀 Odoo 明細記錄要比對哪個欄位——必須是
# `price_subtotal`（未稅），不是 `price_total`（含稅），見模組 docstring
# 「為什麼比對 price_subtotal 而不是 amount_total」那一節。審查修正輪次 1
# Important I3：這個選擇原本是 `cli.py` 裡一個沒有測試守著的字面值
# （`{"fields": ["price_subtotal"]}`），搬成常數讓 `sum_target_amount`
# 的預設值與 `cli.py` 的 RPC 呼叫共用同一個來源，改壞其中一處會被
# 另一處的既有測試連帶絆倒。
TARGET_LINE_AMOUNT_FIELD = "price_subtotal"


@dataclass(frozen=True)
class Check:
    """一條比對結果。

    `kind == "row_count"` 時用 `source_count`／`target_count`，
    `source_amount`／`target_amount` 恆為 `None`；`kind ==
    "amount_subtotal"` 時反過來。兩組欄位刻意都保留在同一個 dataclass
    （而不是拆成兩個子類別），因為 `verification_report.schema.json` 的
    `checks[]` 每一項本來就是同一個扁平形狀，`report.build_report` 不需要
    對兩種 kind 分別組出不同鍵集合的 dict。
    """

    kind: str
    source_table: str
    target_model: str
    status: str
    source_count: int | None
    target_count: int | None
    source_amount: str | None
    target_amount: str | None


def compare_row_count(
    source_table: str, target_model: str, source_count: int, target_count: int
) -> Check:
    """筆數比對：必須完全相等才算 `pass`——筆數沒有「容差」這種東西。

    `source_count` 的定義是「來源表的總筆數」，呼叫端必須用
    `SourceReader.count()` 取得，不可以用「成功轉換的筆數」（例如
    `migration_log.jsonl` 裡非 failed 的列數）代替：載入階段因 `E-4002`
    丟掉一筆時，這裡必須亮 `fail`——分子（Odoo 筆數）分母（來源筆數）
    一起消失、報告顯示全綠，正是計劃 2 那個 Critical 的形狀。
    """
    status = "pass" if source_count == target_count else "fail"
    return Check(
        kind="row_count",
        source_table=source_table,
        target_model=target_model,
        status=status,
        source_count=source_count,
        target_count=target_count,
        source_amount=None,
        target_amount=None,
    )


def compare_amount(
    source_table: str,
    target_model: str,
    source_amount: Decimal,
    target_amount: Decimal,
    *,
    rounding: Decimal,
) -> Check:
    """金額比對：容差以內視為 `pass`（貨幣捨入誤差，不是資料錯誤）。

    `source_amount`／`target_amount` 必須是呼叫端已經用
    `Decimal(str(value))` 建構好的 `Decimal`——這個函式本身不做任何
    float→Decimal 的轉換，也因此無法獨力擋下模組 docstring 說的那個
    地雷；真正的防線在 `sum_source_amount`／`sum_target_amount`。
    """
    status = "pass" if abs(source_amount - target_amount) <= rounding else "fail"
    return Check(
        kind="amount_subtotal",
        source_table=source_table,
        target_model=target_model,
        status=status,
        source_count=None,
        target_count=None,
        source_amount=str(source_amount),
        target_amount=str(target_amount),
    )


# --------------------------------------------------------------------------
# 金額彙總：float 地雷的真正防線
# --------------------------------------------------------------------------


def sum_source_amount(
    rows: Iterable[Mapping[str, Any]], *, qty_column: str, price_column: str
) -> Decimal:
    """來源明細列的 `qty × price` 合計。

    🔴 逐筆都用 `Decimal(str(value))`，不是 `Decimal(value)`——見模組
    docstring 的地雷說明。`rows` 是呼叫端（`cli.py`）已經用
    `SourceReader.rows()` 讀出來的 `dict` 疊代器，這個函式本身不碰
    sqlite，純粹只做加總。
    """
    total = Decimal("0")
    for row in rows:
        qty = Decimal(str(row[qty_column]))
        price = Decimal(str(row[price_column]))
        total += qty * price
    return total


def sum_target_amount(
    records: Iterable[Mapping[str, Any]], *, field: str = TARGET_LINE_AMOUNT_FIELD
) -> Decimal:
    """Odoo 明細記錄的 `price_subtotal` 合計。

    同一個地雷的另一半：Odoo JSON-RPC 回應的數值欄位一樣是 JSON 浮點數，
    `json.loads` 讀回來一樣是 Python `float`，一樣要 `Decimal(str(value))`
    才不會帶進二進位雜訊。`records` 是呼叫端已經用 `client.execute_kw(...,
    "read", ...)` 讀回來的 dict 列表，這個函式不碰 client。
    """
    total = Decimal("0")
    for record in records:
        total += Decimal(str(record[field]))
    return total


def count_names_with_prefix(names: Iterable[str], prefix: str) -> int:
    """數一串 `ir.model.data.name`（已去掉 `migrai.` module 前綴）裡有幾個
    以 `prefix` 開頭。

    純函式：`names` 是呼叫端已經用 `search_read` 查回來的結果，這裡只做
    字串比對，不碰 client。用 python 端逐一 `startswith` 而不是把 prefix
    組進 SQL `LIKE` 的呼叫端邏輯，是為了避開 Odoo domain 的 `=like`
    運算子把 `_`（底線）當成單一字元萬用字元的陷阱——來源表名本身常常
    含底線（`cust_mast`），若直接把 `f"{source_id}.{table}.%"` 當 `=like`
    的樣式送出去，`_` 會被解讀成「任一字元」而不是字面上的底線，理論上
    可能撈進不該撈進的記錄。python 端的 `str.startswith` 沒有這個問題。
    """
    return sum(1 for name in names if name.startswith(prefix))


def res_ids_with_prefix(rows: Iterable[Mapping[str, Any]], prefix: str) -> list[Any]:
    """從 `ir.model.data.search_read` 查回來的 `rows`（每筆含 `name`／
    `res_id` 兩個欄位）裡，挑出 `name` 以 `prefix` 開頭的那些，回傳它們的
    `res_id` 清單（保持原順序）。

    跟 `count_names_with_prefix` 用同一種字串比對（`str.startswith`，同一
    個底線陷阱說明見該函式 docstring），差別只在這裡回傳的是 `res_id`
    清單（供 `cli._target_line_amount` 下一步 `read()` 用），不是計數。
    審查修正輪次 1 Important I3／M38：這段邏輯原本是 `cli.py` 裡一段沒有
    測試守著的 list comprehension，任何人拿掉 `.startswith(prefix)` 這個
    條件（等於把所有 `source_id` 的明細都加進合計）都不會被任何測試擋下；
    搬成獨立函式後可以直接單元測試。
    """
    return [
        row["res_id"]
        for row in rows
        if isinstance(row, Mapping) and str(row.get("name", "")).startswith(prefix)
    ]


def ir_model_data_domain(*, module: str, target_model: str) -> list[list[Any]]:
    """組出查 `ir.model.data` 要用的 domain：限定 `module` 與 `model` 兩個
    條件。

    抽成獨立函式（審查修正輪次 1 Important I3／M44）：`cli.py` 裡
    `_target_names`／`_target_line_amount` 兩處各自組了一份幾乎一樣的
    domain，`model` 條件若被拿掉（例如手滑刪掉那一個 list），查詢會撈到
    「同一個 migrai module 底下、其他目標模型」的記錄，數量對不上卻不會
    有任何測試發現——兩處共用同一個純函式，這裡直接單元測試 domain 的
    形狀，不必連 Odoo 才測得到。
    """
    return [["module", "=", module], ["model", "=", target_model]]


# --------------------------------------------------------------------------
# 從 mapping.json 找出要比對哪些表（純函式，只讀 mapping，不碰 reader／client）
# --------------------------------------------------------------------------


def find_row_count_targets(mapping: Mapping[str, Any]) -> list[tuple[str, str]]:
    """`mapping["models"]` 裡的每一組 `(source_table, target_model)`，
    依表名排序（確定性）。row_count 檢查對每一組都要各做一次。
    """
    return sorted(
        (model["source_table"], model["target_model"]) for model in mapping.get("models", [])
    )


def find_amount_subtotal_target(mapping: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    """在 `mapping["mappings"]` 裡找出哪一張來源表的哪兩個欄位對應到 Odoo
    銷售訂單明細的 `product_uom_qty`／`price_unit`，回傳
    `(source_table, target_model, qty_column, price_column)`；
    找不到（例如 case01 沒有明細表）回傳 `None`。

    用「兩個 Odoo 目標欄位名字同時出現在同一張表」而不是寫死
    `target_model == "sale.order.line"` 來辨認，是因為 `mapping.json`
    只知道目標模型的字面字串（來自 catalog），這裡沒有必要重複假設
    catalog 一定把銷售明細對到這個模型——只要有欄位確實對到這兩個 Odoo
    標準欄位，金額比對就有意義。
    """
    qty_col: dict[tuple[str, str], str] = {}
    price_col: dict[tuple[str, str], str] = {}
    for field_mapping in mapping.get("mappings", []):
        table = field_mapping["source"]["table"]
        model = field_mapping["target"]["model"]
        field = field_mapping["target"]["field"]
        key = (table, model)
        if field == "product_uom_qty":
            qty_col[key] = field_mapping["source"]["column"]
        elif field == "price_unit":
            price_col[key] = field_mapping["source"]["column"]
    for key in sorted(qty_col.keys() & price_col.keys()):
        table, model = key
        return table, model, qty_col[key], price_col[key]
    return None


def resolve_target_counts(
    targets: Iterable[tuple[str, str]],
    source_id: str,
    names_by_model: Mapping[str, Iterable[str]],
) -> dict[str, int]:
    """把「每個目標模型底下所有 `migrai.` 記錄的 name」（`names_by_model`，
    呼叫端已經用 `client.execute_kw` 查回來的原始資料）換算成
    `assemble_checks` 要用的 `target_counts` dict：每張來源表各自數出
    「以 `<編碼後source_id>.<編碼後table>.` 開頭」的筆數。

    純函式：不碰 client，只做字串前綴的組裝與計數（委派給
    `count_names_with_prefix`）。審查修正輪次 1 Critical C1／M57：這段
    「用 `source_id` 與 `table` 組出 prefix 再數」的邏輯原本內嵌在
    `cli._run_verify` 裡，prefix 被換成空字串（等於「同一個目標模型底下
    不分 source_id、不分表，全部都算進去」）也不會被任何測試擋下；
    搬成獨立函式後可以直接餵夾具驗證 prefix 真的有在區隔不同表／不同
    `source_id`。

    最終整分支審查 Critical C1：組 prefix 這裡原本是裸 f-string
    `f"{source_id}.{table}."`，`source_id`／`table` 都沒過
    `planner.encode_pk_value`——但 `build_external_id`（Task 7）寫入
    `migration_log.jsonl`／Odoo 時兩者都過了編碼。只要 `source_id` 或
    `table` 含 `[A-Za-z0-9_]` 以外的字元（例如 CI 整合測試用的
    `task12-full`），這裡組出來的 prefix 就永遠對不上實際寫入的
    external_id，`target_count` 會悄悄變成 0——這正是「絕不悄悄產生錯的
    輸出」要擋的形狀。現在改呼叫 `planner.build_external_id_prefix`，
    跟 `build_external_id` 共用同一顆 `encode_pk_value`，兩邊不會再各自
    漂移。
    """
    return {
        table: count_names_with_prefix(
            names_by_model.get(target_model, ()), build_external_id_prefix(source_id, table)
        )
        for table, target_model in targets
    }


def resolve_synthetic(mapping: Mapping[str, Any]) -> bool:
    """從 `mapping.json` 讀出 `inventory_synthetic`，決定
    `verification_report.json` 的 `synthetic` 欄位——一路從
    `inventory.json` 傳下來（設計 spec §3.6），標記這是合成語料而非真實
    客戶資料（D2）。找不到這個鍵時保守地當成 `False`（不是合成資料）。

    純函式：抽成獨立函式而不是留在 `cli.verify_command` 裡一行
    `bool(mapping_data.get(...))`，是因為那一行沒有任何測試守著——把它
    寫死成 `True`（審查修正輪次 1 Critical C1／M41）不會被任何測試發現，
    這裡直接餵 `inventory_synthetic: False` 的 mapping 驗證回傳值真的是
    `False`。
    """
    return bool(mapping.get("inventory_synthetic", False))


def assemble_checks(
    mapping: Mapping[str, Any],
    source_counts: Mapping[str, int],
    target_counts: Mapping[str, int],
    line_spec: tuple[str, str, str, str] | None,
    source_amount: Decimal | None,
    target_amount: Decimal | None,
    *,
    rounding: Decimal = VERIFICATION_ROUNDING,
) -> list[Check]:
    """把「已經查好的數字」組成完整的 `Check` 清單：對 `mapping` 裡的每一
    張表做 `row_count` 比對，`line_spec` 非 `None` 時再多做一次
    `amount_subtotal` 比對。

    這是這一輪審查修正的核心：`cli._run_verify` 原本自己迴圈組 `Check`、
    自己判斷「要不要做金額比對」，那一整段編排完全沒有測試（審查修正
    輪次 1 Critical C1）。現在 `cli.py` 只負責呼叫 `reader`／`client`
    湊出 `source_counts`／`target_counts`／`source_amount`／
    `target_amount` 這幾個「已經查好的數字」，真正決定「要生出哪些
    `Check`、每個 `Check` 的欄位對到哪個數字」全部收斂在這個純函式——
    `source_counts[table]`／`target_counts[table]` 兩者對調（M31）、
    `line_spec` 非 `None` 卻不生金額 check（M39）、容差用了不是
    `VERIFICATION_ROUNDING` 的值（M42）都會被直接餵給這個函式的測試
    釘住，不必連 Odoo 才測得到。

    `source_counts`／`target_counts` 必須已經含有 `find_row_count_targets
    (mapping)` 裡每一張表的鍵——呼叫端（`cli._run_verify`）保證這一點；
    這裡不另外防呆補預設值 0，缺鍵時讓 `KeyError` 直接浮出比悄悄補一個
    看起來合理但是假的 0 更誠實（原則 6）。
    """
    checks = [
        compare_row_count(table, target_model, source_counts[table], target_counts[table])
        for table, target_model in find_row_count_targets(mapping)
    ]
    if line_spec is not None:
        table, target_model, _qty_column, _price_column = line_spec
        if source_amount is None or target_amount is None:
            raise ValueError(
                "line_spec 非 None 時 source_amount／target_amount 必須都已提供——"
                "呼叫端應該已經用 sum_source_amount／_target_line_amount 算好這兩個數字"
            )
        checks.append(
            compare_amount(table, target_model, source_amount, target_amount, rounding=rounding)
        )
    return checks


def fatal_code_for(check: Check) -> ErrorCode:
    """依 `Check.kind` 決定失敗時要發哪個致命碼：`row_count` → E-5001
    （TOTAL_MISMATCH）、`amount_subtotal` → E-5002（BALANCE_ERROR）——
    spec §3.1 的退出碼契約表就是這樣寫的，兩種檢查失敗要能從錯誤碼分辨
    是「筆數對不上」還是「金額對不上」。

    審查修正輪次 1 Critical C2／M34：這個判斷原本內嵌在 `cli.
    verify_command` 的迴圈裡，`E_5001`／`E_5002` 對調（兩種 kind 各自
    發錯碼）不會被任何測試擋下；搬成獨立函式後可以直接對兩種 `kind`
    各餵一個 `Check` 驗證。
    """
    return E_5001 if check.kind == "row_count" else E_5002


# --------------------------------------------------------------------------
# --log（migration_log.jsonl）的解析：純函式，只吃字串
# --------------------------------------------------------------------------

_LOG_SCHEMA_PATH = Path(__file__).parent / "schema" / "migration_log.schema.json"
_LOG_SCHEMA = json.loads(_LOG_SCHEMA_PATH.read_text(encoding="utf-8"))
_LOG_VALIDATOR = Draft202012Validator(_LOG_SCHEMA)


class MigrationLogReadError(Exception):
    """`--log` 內容不是合法的 `migration_log.jsonl`。

    這是外部檔案（可能是舊版、手改過、或根本是別的檔案）——不能讓裸
    `json.JSONDecodeError`／`jsonschema.ValidationError` 逃出模組職責契約
    （全域約束原則 6，Task 10 審查修正輪次 1 Critical C1 同型缺陷）。
    """


def parse_migration_log(text: str) -> list[dict[str, Any]]:
    """把 `--log` 檔案的原始內容（呼叫端已經讀成字串）解析成一串 dict，
    每一行都用 `migration_log.schema.json` 驗證過。

    純函式：只吃字串，不吃路徑——檔案本身讀不讀得到是 `cli.py` 的責任
    （`OSError`），這裡只管「這串文字合不合法」。空白行（含檔案末尾的
    換行造成的空字串）直接略過，不視為錯誤；`migration_log.jsonl` 完全
    沒有任何內容（例如載入階段因 `plan_load` 一開始就失敗而一筆都沒寫，
    見 `loader._MigrationLogWriter` docstring）是合法狀態，回傳空清單。
    """
    entries: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MigrationLogReadError(f"第 {lineno} 行不是合法的 JSON：{exc}") from exc
        try:
            _LOG_VALIDATOR.validate(payload)
        except ValidationError as exc:
            raise MigrationLogReadError(
                f"第 {lineno} 行不符合 migration_log.schema.json：{exc.message}"
            ) from exc
        entries.append(payload)
    return entries


def find_mismatched_external_ids(entries: Iterable[Mapping[str, Any]], *, prefix: str) -> list[str]:
    """回傳 `entries`（`parse_migration_log` 的產出）裡 `external_id` **不是**
    以 `prefix` 開頭的那些 external_id（依原順序，可能重複）。

    審查修正輪次 1 Critical C4／控制者裁決：`--log` 原本是「幾乎無作用的
    必填參數」——`verify_command` 印出「對照的載入紀錄」這行字暗示了一個
    對應關係，但程式碼裡沒有任何一行真的檢查過。最低成本的守衛：`--log`
    裡每一筆 `external_id` 都該以 `migrai.<source_id>.` 開頭（`planner.
    build_external_id` 的字面契約），不符代表這份 log 根本不是這次
    `--source-id` 對應的那次載入——純字串比對，不需要連 Odoo。

    刻意不在這裡把 `"migrai."` 這個 module 段寫死：`prefix` 由呼叫端
    （`cli.py`，用 `planner.build_external_id_module_prefix(source_id)`
    組好，跟 `build_external_id` 寫入時同一套 `encode_pk_value`——最終
    整分支審查 Critical C1：舊版呼叫端是裸 f-string，沒有編碼，`source_id`
    含特殊字元時會誤判合法的 log 為不符）再傳進來，這個函式只管字串
    比對，不重複假設模組前綴的字面值或編碼規則。
    """
    return [
        str(entry.get("external_id", ""))
        for entry in entries
        if not str(entry.get("external_id", "")).startswith(prefix)
    ]
