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

# --------------------------------------------------------------------------
# Check：一條比對結果
# --------------------------------------------------------------------------

# 兩個 kind、兩個 status，意義互斥且窮盡（precedent：models.ACTIONS）。
KINDS = frozenset({"row_count", "amount_subtotal"})
STATUSES = frozenset({"pass", "fail"})


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
    records: Iterable[Mapping[str, Any]], *, field: str = "price_subtotal"
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
