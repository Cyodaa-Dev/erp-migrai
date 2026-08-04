"""載入階段：把 `plan_load` 排好的計畫真的寫進 Odoo，寫出
`migration_log.jsonl`。

## 寫入方式：Odoo 內建 `load()`（spec P3-7 已拍板）

`load(fields, data)` 的冪等（依 external id 決定 create 或 update）與外鍵
解析（`<field>/id` 欄位路徑）都是 Odoo 本來就有的能力，這裡不自己
`create` + 寫 `ir.model.data`——自造等於重寫一遍 Odoo 的匯入語意，而且會
與使用者從 UI 匯入 CSV 的結果分岔。

## `load()` 分不出 created／updated：這裡多付出一次 RPC 換誠實

查證過 `odoo/models.py:1145` 的 `load()`：回傳只有 `{ids, messages}`，
**不**告訴呼叫端「這筆是新建還是更新」。`migration_log` 的 `action` 卻
明確要求 `created`／`updated` 兩個互斥的值——把 update 報成 created 是
「悄悄產生錯的輸出」。因此每一批寫入前，這裡先用 `ir.model.data.
search_read` 查一次「這批 external_id 裡哪些已經存在」（`_existing_
external_id_names`），查完才知道接下來 `load()` 會建立還是覆寫哪一筆。
**這是縱深防禦的代價：每批多一次 RPC**——Task 12 算 RPC 次數／逾時預算
時要把這次也算進去。這次查詢一律只讀（`search_read`），不 `create`、不
寫 `ir.model.data`；寫入仍然只走 `load()`。

## `load()` 是全有全無的：一筆錯，整批連同已成功的一起回滾

查證過 `odoo/models.py` 的 `load()` 原始碼（`1145-1313` 行）：它內部確實
會先嘗試整批 `_load_records`、失敗後改成逐筆 `_load_records` 並把逐筆的
錯誤蒐集進 `messages`——但函式**最後**還有一段：只要 `messages` 裡出現
任何一則 `type == "error"`，就 `sp.rollback()` 把整個 savepoint 回滾、
`ids` 整個改寫成 `False`。也就是說**單次 `load()` 呼叫是全有全無的**：
一批 100 筆裡只要有 1 筆壞掉，其餘 99 筆已經成功建立的記錄也會被一起
回滾，`result["ids"]` 回傳 `False`，`result["messages"]` 才有錯誤細節。

這正是 spec 要求「整批失敗 → 發 E-4004（WARN）並降級逐筆以隔離」的原因：
Odoo 自己內部的批次/逐筆混合重試機制**不會**把好的記錄留下——要達成
「好的照樣進去，壞的記進 migration_log」，必須是這一層（loader.py）自己
在整批呼叫失敗（不論是 `ids is False` 還是 RPC 層直接拋 `OdooError`）之後
再發起 N 次獨立的單筆 `load()` 呼叫，讓每一筆各自擁有自己的 savepoint。

## 架構鐵律

這個檔案**絕不** import `urllib`／`http`／`socket`／`sqlite3` 等傳輸層
模組——`tests/pipeline/test_odoo_client.py::test_only_odoo_client_imports_
network_modules` 已經在掃描 `src/erp_migrai/` 底下每一個檔案（含這個新
檔案），任何違反都會讓那條測試紅。Odoo 連線一律透過注入進來的
`OdooClient`，只認 `OdooError`／`OdooAuthError`／`RateLimited` 三種例外。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from erp_migrai.events import E_4001, E_4002, E_4003, E_4004, E_4005, EventLog
from erp_migrai.pipeline.models import LogEntry
from erp_migrai.pipeline.odoo_client import OdooAuthError, OdooClient, OdooError, RateLimited
from erp_migrai.pipeline.planner import (
    CyclicReference,
    TablePlan,
    UnsupportedReference,
    build_external_id,
    plan_load,
)
from erp_migrai.pipeline.source import SourceError, SourceReader
from erp_migrai.pipeline.transform import TransformError, apply_transform

MIGRATION_LOG_FILENAME = "migration_log.jsonl"

SCHEMA_PATH = Path(__file__).parent / "schema" / "migration_log.schema.json"
_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)

# `ir.model.data` 的 module 段：`build_external_id` 產出 `migrai.<source_id>.
# <table>.<pk>`，Odoo 用 `xmlid.split('.', 1)` 拆，所以 module 恆為這個字面
# 值，`name` 是拿掉這段之後剩下的部分。
_MODULE = "migrai"

# 指數退避的基礎延遲（秒）：第 N 次重試前等待 `_BASE_DELAY_SECONDS * 2**(N-1)`。
_BASE_DELAY_SECONDS = 1.0


class LogEntrySchemaError(Exception):
    """loader 自己組出的一筆 `migration_log` 記錄不符合
    `migration_log.schema.json`——這是 loader 自己的缺陷，不是使用者輸入
    的問題（precedent：`mapping/writer.py` 的 `MappingSchemaError`）。
    """


# ---------------------------------------------------------------------------
# retry_with_backoff：收到 RateLimited 時指數退避重試
# ---------------------------------------------------------------------------


def retry_with_backoff[T](
    call: Callable[[], T],
    *,
    attempts: int,
    sleep: Callable[[float], None],
    on_rate_limit: Callable[[int, float], None],
) -> T:
    """收到 `RateLimited` 時指數退避重試。`sleep` 可注入，測試才不會真的睡。

    只重試 `RateLimited`——`OdooError`（含其子類別 `OdooAuthError`）不重試：
    重試一個必然失敗的呼叫只會把錯誤延後好幾倍的時間才浮出來，對認證錯誤
    或資料驗證錯誤沒有「等一下再試就會成功」的道理。

    `attempts` 是總嘗試次數的上限（不是「重試次數」）：`attempts=3` 代表
    最多呼叫 `call()` 三次，前兩次失敗才重試，第三次失敗就不再重試、直接
    讓 `RateLimited` 往外拋。

    第 N 次失敗後、第 N+1 次重試前，延遲是 `_BASE_DELAY_SECONDS *
    2 ** (N-1)`（1, 2, 4, 8, ... 秒）——`on_rate_limit(attempt, delay)` 在
    呼叫 `sleep(delay)` 之前呼叫，讓呼叫端（`loader.py` 內部）能據此發出
    E-4005 事件，帶著「這是第幾次」與「等了多久」兩個資訊。
    """
    if attempts < 1:
        raise ValueError(f"attempts 必須 >= 1，收到 {attempts}")
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except RateLimited:
            if attempt == attempts:
                raise
            delay = _BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            on_rate_limit(attempt, delay)
            sleep(delay)
    # 迴圈的每一輪要嘛 return、要嘛（最後一輪）raise，理論上跑不到這裡；
    # 保留這一行讓型別檢查器與讀者都清楚這不是遺漏分支（precedent：
    # odoo_client.py 的 `_send()` 對 unreachable 分支的處理方式）。
    raise AssertionError("unreachable：retry_with_backoff 迴圈必定 return 或 raise")


# ---------------------------------------------------------------------------
# migration_log.jsonl 的逐筆驗證與寫出
# ---------------------------------------------------------------------------


def _validate_and_dump(entry: LogEntry) -> str:
    payload = entry.to_dict()
    try:
        _VALIDATOR.validate(payload)
    except ValidationError as exc:
        raise LogEntrySchemaError(
            "loader 組出的 migration_log 記錄不符合 migration_log.schema.json"
            f"（這是 loader 的缺陷，不是你的輸入有問題，請回報 issue）：{exc.message}"
        ) from exc
    return json.dumps(payload, ensure_ascii=False)


class _MigrationLogWriter:
    """把 `LogEntry` 逐筆驗證後 append 進 `migration_log.jsonl`。

    每次執行都從乾淨檔案開始並立刻 touch 出空檔案（precedent：
    `cli._fresh_event_log` 對 `events.jsonl` 的處理）：即使這次執行因為
    `plan_load` 一開始就撞見 `CyclicReference` 而一筆記錄都沒寫，
    `migration_log.jsonl` 這個檔案仍然要存在，維持「每次執行都會寫入
    這個檔案」的介面承諾。
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.touch()
        self.path = path

    def append(self, entry: LogEntry) -> None:
        line = _validate_and_dump(entry)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# 單筆記錄的本地決策：轉換、external_id、懸空外鍵偵測、dry-run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PendingRow:
    """通過本地檢查（轉換成功、沒有懸空外鍵、不是 dry-run），真的要送進
    Odoo `load()` 的一筆。"""

    external_id: str
    name_without_prefix: str
    source_pk: tuple[str, ...]
    values: list[str]


def _name_without_module_prefix(external_id: str) -> str:
    """`external_id` 恆為 `migrai.<rest>`（`build_external_id` 的契約），
    查詢 `ir.model.data` 時 `name` 要用不含 `migrai.` 前綴的那一段（見
    Odoo `xmlid.split('.', 1)` 的解析方式：module 只取第一個點之前）。
    """
    return external_id.split(".", 1)[1]


def _failed_entry(
    *,
    run_id: str,
    table: str,
    source_pk: tuple[str, ...],
    external_id: str,
    target_model: str,
    message: str,
) -> LogEntry:
    return LogEntry(
        run_id=run_id,
        source_table=table,
        source_pk=source_pk,
        external_id=external_id,
        target_model=target_model,
        action="failed",
        odoo_id=None,
        error_code="E-4002",
        message=message,
    )


def _row_value(row: dict[str, Any], column: str, *, where: str) -> Any:
    """直接索引 `row[column]`，缺欄位時轉成點名 `where` 的 `TransformError`
    （precedent：`transform.py` 的 `_require_key`）。

    這是**縱深防禦**，不是主要防線：主要防線是 `run_load` 進主迴圈前
    用 `reader.columns()` 對 `table_plan.columns` 做的整表比對
    （`_missing_source_columns`）——`--mapping` 與 `--source` 是兩個獨立
    的 CLI 參數，沒有機制保證配對，那一層已經能一次擋下「mapping 提到的
    欄位在來源資料庫不存在」這個最常見的形狀。但這一層不能只靠上游
    守住：`SourceReader` 是這個專案自己定義的介面，不保證每個實作（含
    未來的商業版讀取器）在 `rows()` 裡吐出的每一列都恰好含有
    `columns()` 回報過的每一個欄位——裸 `KeyError` 逃出去會是使用者
    直接看到的 traceback（Task 10 審查修正輪次 1 Critical C1）。
    """
    try:
        return row[column]
    except KeyError:
        raise TransformError(f"{where}：來源列缺少欄位 {column!r}") from None


def _decide_row(
    row: dict[str, Any],
    table_plan: TablePlan,
    pk_cols: list[str],
    *,
    source_id: str,
    run_id: str,
    dry_run: bool,
    problem_external_ids: set[str],
    event_log: EventLog,
) -> LogEntry | _PendingRow:
    """決定單一來源列的命運：`failed`（E-4002）、`skipped`（上游失敗或
    dry-run），或者「通過本地檢查，等著被送進 Odoo」（`_PendingRow`）。

    `problem_external_ids` 只在這個函式裡被讀取與寫入——它累積的是**因
    TransformError 而失敗**（或本身也是因上游失敗而 skipped）的
    external_id 集合，不包含 Odoo 實際拒絕寫入（E-4003）的記錄。這是刻意
    的範圍縮減：spec §3.5 明講「skipped 存在的理由：根因那一筆已經發過
    E-4002 了」，只把 skip 級聯綁在 E-4002 這條根因上；E-4003 只有在真的
    嘗試呼叫 `load()` 之後才知道，那時候同一個批次／後續批次的其他列
    的本地決策早就做完了，要讓 E-4003 也級聯需要重新設計批次與決策的
    先後順序，超出這次的範圍（見報告「刻意沒做的事」）。
    """
    table = table_plan.source_table
    try:
        pk_values = [_row_value(row, c, where=table) for c in pk_cols]
    except TransformError as exc:
        # 縱深防禦命中：連主鍵欄位本身都缺席，組不出任何有意義的
        # external_id／source_pk（`LogEntry` 要求 `source_pk` 非空）。
        # 這條理論上到不了（`pk_cols` 來自 `reader.primary_key(table)`，
        # 保證是這張表真實存在的欄位；主要防線也已經在 run_load 進主
        # 迴圈前擋過一次），用固定占位字串充當診斷用途，這筆本來就到
        # 不了 Odoo。
        message = f"{exc}，此筆不會被載入"
        event_log.emit(
            "load.transform_error",
            code=E_4002,
            message=message,
            details={"source_table": table},
        )
        external_id = f"migrai.{source_id}.{table}.ERROR"
        problem_external_ids.add(external_id)
        return _failed_entry(
            run_id=run_id,
            table=table,
            source_pk=("ERROR",),
            external_id=external_id,
            target_model=table_plan.target_model,
            message=message,
        )
    source_pk = tuple(str(v) for v in pk_values)

    try:
        external_id = build_external_id(source_id, table, pk_values)
    except ValueError as exc:
        # 主鍵值本身有問題（含 NULL）：這一筆永遠不會被送進 Odoo，這裡
        # 組出的 external_id 只是給人看的診斷用途，不需要滿足
        # encode_pk_value 的無歧義保證（那個保證是為了 Odoo 那邊不撞
        # id，這筆根本到不了那裡）。
        external_id = f"migrai.{source_id}.{table}." + "-".join(str(v) for v in pk_values)
        message = f"{table} 這一筆的主鍵值無法組出合法的 external_id（{exc}），此筆不會被載入"
        event_log.emit(
            "load.transform_error",
            code=E_4002,
            message=message,
            details={"source_table": table, "source_pk": list(source_pk)},
        )
        problem_external_ids.add(external_id)
        return _failed_entry(
            run_id=run_id,
            table=table,
            source_pk=source_pk,
            external_id=external_id,
            target_model=table_plan.target_model,
            message=message,
        )

    values_row: list[str] = []
    transform_error: TransformError | None = None
    for col in table_plan.columns:
        try:
            raw_value = _row_value(row, col.source_column, where=f"{table}.{col.source_column}")
            value = apply_transform(
                col.transform, raw_value, source_id=source_id, where=f"{table}.{col.source_column}"
            )
        except TransformError as exc:
            transform_error = exc
            break
        values_row.append(value)

    if transform_error is not None:
        message = str(transform_error)
        event_log.emit(
            "load.transform_error",
            code=E_4002,
            message=message,
            details={"source_table": table, "external_id": external_id},
        )
        problem_external_ids.add(external_id)
        return _failed_entry(
            run_id=run_id,
            table=table,
            source_pk=source_pk,
            external_id=external_id,
            target_model=table_plan.target_model,
            message=message,
        )

    # 懸空外鍵偵測：任何 reference 欄位解析出的 external_id 若已經在
    # problem_external_ids 裡，代表它指向的來源記錄本身轉換失敗（或本身
    # 也是因為上游失敗而被 skip），寫下去只會產生一筆掛在錯誤父記錄上的
    # 孤兒。空字串（NULL 外鍵，`apply_transform` 對 reference 的 None 已
    # 回傳 ""）不算懸空，那是合法的「沒有關聯」。
    skip_parent: str | None = None
    for col, value in zip(table_plan.columns, values_row, strict=True):
        if col.transform.get("kind") == "reference" and value and value in problem_external_ids:
            skip_parent = value
            break

    if skip_parent is not None:
        message = f"上游記錄 {skip_parent} 未成功寫入（轉換失敗，或其上游本身失敗），此筆略過寫入"
        problem_external_ids.add(external_id)
        return LogEntry(
            run_id=run_id,
            source_table=table,
            source_pk=source_pk,
            external_id=external_id,
            target_model=table_plan.target_model,
            action="skipped",
            odoo_id=None,
            error_code=None,
            message=message,
        )

    if dry_run:
        # 走完讀取、轉換、排序、組值的全部路徑，只是不連 Odoo——這一筆
        # 若不是 dry-run 會被寫入，但這裡誠實標成 skipped，不偽稱
        # created／updated（那兩個值必須真的有 Odoo 回應的 id 才能標）。
        # 刻意不加進 problem_external_ids：若這是真正的一次執行，這筆會
        # 成功寫入，不該讓下游行因為「dry-run 沒連線」而被誤判成
        # 「上游失敗」。
        return LogEntry(
            run_id=run_id,
            source_table=table,
            source_pk=source_pk,
            external_id=external_id,
            target_model=table_plan.target_model,
            action="skipped",
            odoo_id=None,
            error_code=None,
            message="dry-run：未連線 Odoo，本筆尚未實際嘗試寫入",
        )

    return _PendingRow(
        external_id=external_id,
        name_without_prefix=_name_without_module_prefix(external_id),
        source_pk=source_pk,
        values=values_row,
    )


# ---------------------------------------------------------------------------
# 真的呼叫 Odoo：existing 查詢、整批 load()、降級逐筆
# ---------------------------------------------------------------------------


def _existing_external_id_names(client: OdooClient, names: list[str]) -> set[str]:
    """查一次 `ir.model.data`，回傳這批 external_id（已去掉 `migrai.` 前綴）
    裡哪些已經存在於 Odoo。這是一次「讀」，不是「寫」——寫入一律走
    `client.execute_kw(..., "load", ...)`，這裡只用 `search_read` 查詢，
    不 `create`、不寫 `ir.model.data`。
    """
    if not names:
        return set()
    rows = client.execute_kw(
        "ir.model.data",
        "search_read",
        [[["module", "=", _MODULE], ["name", "in", list(names)]]],
        {"fields": ["name"]},
    )
    return {row["name"] for row in rows}


def _first_error_message(messages: object) -> str | None:
    """從 `load()` 回傳的 `messages` 裡挑一則最有用的錯誤訊息。

    優先找 `type == "error"` 的那一則（`type == "warning"` 不代表這筆
    寫入失敗）；找不到就退而求其次取第一則的內容；`messages` 形狀不符
    預期（不是 list，或內容不是 dict）一律回傳 `None`，不讓裸
    `KeyError`／`TypeError` 逃出去。
    """
    if not isinstance(messages, list) or not messages:
        return None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("type") == "error" and msg.get("message"):
            return str(msg["message"])
    first = messages[0]
    if isinstance(first, dict) and first.get("message"):
        return str(first["message"])
    return None


def _emit_load_failed(event_log: EventLog, *, table: str, external_id: str, message: str) -> None:
    event_log.emit(
        "load.load_failed",
        code=E_4003,
        message=f"{table}：{message}",
        details={"source_table": table, "external_id": external_id},
    )


def _on_rate_limit(event_log: EventLog, *, table: str, mode: str) -> Callable[[int, float], None]:
    def _handler(attempt: int, delay: float) -> None:
        event_log.emit(
            "load.rate_limit_hit",
            code=E_4005,
            message=f"{table} {mode}遇到限流，第 {attempt} 次重試前等待 {delay:.1f} 秒",
            details={"source_table": table, "mode": mode},
        )

    return _handler


def _emit_batch_retry(event_log: EventLog, *, table: str, reason: str, batch_size: int) -> None:
    """整批失敗、即將降級逐筆時發 E-4004（WARN）。

    與 `_emit_load_failed`／`_on_rate_limit` 一樣是**純本地**的發碼點：
    只吃 `EventLog`，不吃 client。複驗輪次 2 指出這三個發碼點原本零守衛
    ——E-4003／E-4004／E-4005 是對下游（Task 11 verify、商業版 log
    consumer）承諾的錯誤碼契約，發錯碼會讓「限流」與「整批失敗」互換，
    而兩者的處置方式完全不同。抽成同型的具名函式之後就測得到。
    """
    event_log.emit(
        "load.batch_retry",
        code=E_4004,
        message=f"{table}：{reason}，降級為逐筆載入以隔離壞掉的記錄（本批 {batch_size} 筆）",
        details={"source_table": table, "batch_size": batch_size},
    )


def _failed_entries(
    pending: Sequence[_PendingRow],
    *,
    table_plan: TablePlan,
    run_id: str,
    message: str,
) -> list[LogEntry]:
    """把一批（或一筆）待寫入的列全部標成 `failed`／E-4003。

    複驗輪次 2 的 Minor：這段「建一串 failed LogEntry」在
    `_flush_individually` 的兩個 except 分支與 `_flush_batch` 的
    `OdooAuthError` 分支裡是**逐字重複的複製貼上**，而且
    `action="failed"` 改成 `"skipped"` 沒有任何測試會紅——正是 I6 剛
    修掉的形狀又長回來一份。它是純本地邏輯（吃 `pending`／`table_plan`／
    `run_id`／`message`，不吃 client），與 `_entries_from_ids` 同一類，
    所以提到模組層共用並配單元測試。
    """
    return [
        LogEntry(
            run_id=run_id,
            source_table=table_plan.source_table,
            source_pk=item.source_pk,
            external_id=item.external_id,
            target_model=table_plan.target_model,
            action="failed",
            odoo_id=None,
            error_code="E-4003",
            message=message,
        )
        for item in pending
    ]


# ---------------------------------------------------------------------------
# 純函式：created／updated 決策與批次結果判讀
#
# 這三個函式的存在理由是 Task 10 審查修正輪次 1 Important I6：`_flush_
# batch`／`_flush_individually` 唯一的職責是呼叫 `client.execute_kw()`
# （誠實界線之外，見模組頂部與 `tests/pipeline/test_loader.py` docstring），
# 但「拿到 Odoo 已經回應的 ids／existing 之後該怎麼分類每一列」是純本地
# 邏輯，不需要 Odoo 存在——被埋在呼叫 execute_kw 的函式內聯迴圈裡，是
# 結構問題，不是誠實界線問題（precedent：`_merge_decisions` 就是為了
# 可測性才從 `run_load` 內聯迴圈抽出來的）。抽出來之後，
# `_flush_batch`／`_flush_individually` 剩下的就只是「呼叫 execute_kw
# 並把回傳值交給這幾個純函式」，那部分才是真的留給 Task 12 的範圍。
# ---------------------------------------------------------------------------


def _action_for(name_without_prefix: str, existing: set[str]) -> str:
    """單一記錄該標 `created` 還是 `updated`。

    `existing` 是 `_existing_external_id_names` 查到的、這批裡已經存在於
    Odoo 的 name 集合——這裡只讀不寫，純函式。
    """
    return "updated" if name_without_prefix in existing else "created"


def _entries_from_ids(
    pending: list[_PendingRow],
    ids: list[Any],
    existing: set[str],
    *,
    table_plan: TablePlan,
    run_id: str,
) -> list[LogEntry]:
    """把 Odoo `load()` 回傳的 `ids`（依 `pending` 的順序一一對應，
    `_batch_outcome` 已經確認過兩者長度相等）轉成一批 `LogEntry`。

    純函式：`ids`／`existing` 都是呼叫端已經拿到（或測試手工建構）的
    資料，不是假扮 Odoo 回話（precedent：`_first_error_message` 手工建構
    `messages` payload 餵給純函式的測法）。`zip(..., strict=True)` 是
    最後一道防線——長度不符本來就該在呼叫 `_batch_outcome` 那一步被擋下，
    這裡用 `strict=True` 確保萬一沒被擋下也不會安靜地錯位配對。
    """
    entries: list[LogEntry] = []
    for item, odoo_id in zip(pending, ids, strict=True):
        entries.append(
            LogEntry(
                run_id=run_id,
                source_table=table_plan.source_table,
                source_pk=item.source_pk,
                external_id=item.external_id,
                target_model=table_plan.target_model,
                action=_action_for(item.name_without_prefix, existing),
                odoo_id=odoo_id,
                error_code=None,
                message="",
            )
        )
    return entries


def _batch_outcome(result: Any, *, expected_count: int) -> tuple[bool, str]:
    """判斷整批 `load()` 的回傳值算成功還是失敗，回傳
    `(成功?, 失敗理由)`（成功時第二個值是空字串）。

    純函式：`result` 是 `load()` 已經回傳（或測試手工建構）的
    `{ids, messages}` 形狀 payload，不是假扮 Odoo 回話。

    兩種失敗各自給出不同訊息，方便 `_degrade` 的 E-4004 事件點名原因：

    - `ids` 是 falsy（`False`、`None`、空 list……）：模組 docstring 記錄
      的 Odoo `load()` 行為——一批裡只要有一筆錯，`ids` 整個回傳
      `False`，不是「這幾筆成功、那幾筆失敗」的部分結果。
    - `ids` 長度與 `expected_count`（送出的列數）不符：Odoo 的逐筆重試
      迴圈在 `psycopg2.Warning` 分支只 append `messages`、不 append
      `ids`、也不算進 `errors`，導致 `ids` 是一個比輸入短、但仍是
      truthy 的 list（Task 10 審查修正輪次 1 Important I7）——這裡在
      呼叫端動手 `zip(..., strict=True)` 之前就先擋下，讓使用者看到的
      是清楚點名數字對不上的 E-4004，而不是一個毫無資訊量的
      `ValueError: zip() argument 2 is shorter than argument 1`。
    """
    ids = result.get("ids") if isinstance(result, dict) else None
    if not ids:
        return False, "Odoo 拒絕整批寫入"
    if len(ids) != expected_count:
        return False, f"Odoo 回傳的 id 數量（{len(ids)}）與送出的列數（{expected_count}）不符"
    return True, ""


def _flush_individually(
    pending: list[_PendingRow],
    table_plan: TablePlan,
    client: OdooClient,
    *,
    existing: set[str],
    run_id: str,
    event_log: EventLog,
    attempts: int,
    sleep: Callable[[float], None],
) -> list[LogEntry]:
    """逐筆呼叫 `load()`，每一筆各自擁有自己的 savepoint（見模組
    docstring：這是隔離壞記錄唯一的辦法）。"""
    field_names = ["id"] + [c.odoo_field for c in table_plan.columns]
    entries: list[LogEntry] = []
    for item in pending:
        data_matrix = [[item.external_id, *item.values]]

        def _call(data_matrix: list[list[str]] = data_matrix) -> Any:
            return client.execute_kw(table_plan.target_model, "load", [field_names, data_matrix])

        try:
            result = retry_with_backoff(
                _call,
                attempts=attempts,
                sleep=sleep,
                on_rate_limit=_on_rate_limit(
                    event_log, table=table_plan.source_table, mode="逐筆載入"
                ),
            )
        except OdooError as exc:
            message = str(exc)
            _emit_load_failed(
                event_log,
                table=table_plan.source_table,
                external_id=item.external_id,
                message=message,
            )
            entries.extend(
                _failed_entries([item], table_plan=table_plan, run_id=run_id, message=message)
            )
            continue

        ids = result.get("ids") if isinstance(result, dict) else None
        if not ids:
            message = (
                _first_error_message(result.get("messages") if isinstance(result, dict) else None)
                or "Odoo 拒絕寫入，未提供詳細訊息"
            )
            _emit_load_failed(
                event_log,
                table=table_plan.source_table,
                external_id=item.external_id,
                message=message,
            )
            entries.extend(
                _failed_entries([item], table_plan=table_plan, run_id=run_id, message=message)
            )
            continue

        odoo_id = ids[0]
        action = _action_for(item.name_without_prefix, existing)
        entries.append(
            LogEntry(
                run_id=run_id,
                source_table=table_plan.source_table,
                source_pk=item.source_pk,
                external_id=item.external_id,
                target_model=table_plan.target_model,
                action=action,
                odoo_id=odoo_id,
                error_code=None,
                message="",
            )
        )
    return entries


def _flush_batch(
    pending: list[_PendingRow],
    table_plan: TablePlan,
    client: OdooClient,
    *,
    run_id: str,
    event_log: EventLog,
    attempts: int,
    sleep: Callable[[float], None],
) -> list[LogEntry]:
    """把一批 `_PendingRow` 真的送進 Odoo。

    先查一次這批 external_id 裡哪些已存在（決定 created／updated），再嘗試
    整批 `load()`；整批失敗（不論是 RPC 層拋 `OdooError`，還是 `load()`
    正常回傳但 `ids` 是 `False`，見模組 docstring）就發 E-4004（WARN）並
    降級逐筆，好記錄照樣進去，壞記錄各自被隔離出來。
    """
    if not pending:
        return []

    names = [item.name_without_prefix for item in pending]
    try:
        existing = _existing_external_id_names(client, names)
    except OdooError as exc:
        # 連「查詢既有記錄」這一步都失敗，通常代表 Odoo 端本身不可達或
        # 拒絕連線——後續的 load() 呼叫十之八九也會遇到同樣的問題。與其
        # 讓例外逃出這個函式（run_load 的外層 try/except 只認
        # SourceError／CyclicReference／UnsupportedReference，OdooError
        # 逃出去會變成 CLI 看不懂的裸例外），這裡直接把整批標成 E-4003
        # 失敗，誠實地結束這一批。
        message = f"查詢既有 external_id 失敗：{exc}"
        _emit_load_failed(
            event_log, table=table_plan.source_table, external_id="(batch)", message=message
        )
        return [
            LogEntry(
                run_id=run_id,
                source_table=table_plan.source_table,
                source_pk=item.source_pk,
                external_id=item.external_id,
                target_model=table_plan.target_model,
                action="failed",
                odoo_id=None,
                error_code="E-4003",
                message=message,
            )
            for item in pending
        ]

    # 批次大小恰好 1：批次呼叫與逐筆呼叫是同一次 RPC，直接走逐筆路徑，
    # 不重複呼叫一次注定得到相同結果的 load()，也不會為單一記錄誤發一則
    # 語意不清的「整批失敗」事件。
    if len(pending) == 1:
        return _flush_individually(
            pending,
            table_plan,
            client,
            existing=existing,
            run_id=run_id,
            event_log=event_log,
            attempts=attempts,
            sleep=sleep,
        )

    field_names = ["id"] + [c.odoo_field for c in table_plan.columns]
    data_matrix = [[item.external_id, *item.values] for item in pending]

    def _call() -> Any:
        return client.execute_kw(table_plan.target_model, "load", [field_names, data_matrix])

    def _degrade(reason: str, *, individual_attempts: int = attempts) -> list[LogEntry]:
        _emit_batch_retry(
            event_log,
            table=table_plan.source_table,
            reason=reason,
            batch_size=len(pending),
        )
        return _flush_individually(
            pending,
            table_plan,
            client,
            existing=existing,
            run_id=run_id,
            event_log=event_log,
            attempts=individual_attempts,
            sleep=sleep,
        )

    def _batch_failed_entries(message: str) -> list[LogEntry]:
        """整批直接標成失敗，不降級逐筆——目前只有 `OdooAuthError` 會走
        這條（見下方 `except OdooAuthError` 分支的理由）。

        建 LogEntry 的部分委派給模組層的 `_failed_entries`，與
        `_flush_individually` 的兩個失敗分支共用同一份邏輯（複驗輪次 2
        的 Minor：原本是逐字重複的複製貼上，`action` 改成 `"skipped"`
        沒有任何測試會紅）。
        """
        _emit_load_failed(
            event_log, table=table_plan.source_table, external_id="(batch)", message=message
        )
        return _failed_entries(pending, table_plan=table_plan, run_id=run_id, message=message)

    try:
        result = retry_with_backoff(
            _call,
            attempts=attempts,
            sleep=sleep,
            on_rate_limit=_on_rate_limit(event_log, table=table_plan.source_table, mode="批次載入"),
        )
    except OdooAuthError as exc:
        # M4：`OdooAuthError` 與 `RateLimited` 都是 `OdooError` 子類，一視
        # 同仁降級逐筆的話，認證失敗（帳密錯、資料庫不存在）不會因為
        # 換成逐筆呼叫就變好——100 筆的批次會變成 100 次注定失敗的無用
        # RPC。認證問題整批直接標成失敗，不進 `_flush_individually`。
        return _batch_failed_entries(f"Odoo 認證失敗：{exc}")
    except RateLimited as exc:
        # M4：整批呼叫在走到這裡之前，`retry_with_backoff` 已經照
        # `attempts` 次指數退避重試過一輪（`attempts=5` 時 ≈ 1+2+4+8=15
        # 秒）才會讓 `RateLimited` 逃出來——若降級逐筆時每一筆都重新給滿
        # `attempts`，退避時間會被放大 `len(pending)` 倍（100 筆 ×
        # 15 秒 ≈ 25 分鐘）。已經知道 Odoo 現在在限流，逐筆改成只嘗試
        # 一次（不再重新退避），讓這筆盡快標成失敗，而不是重複等待。
        return _degrade(str(exc), individual_attempts=1)
    except OdooError as exc:
        return _degrade(str(exc))

    ok, reason = _batch_outcome(result, expected_count=len(pending))
    if not ok:
        return _degrade(reason)

    ids = result["ids"] if isinstance(result, dict) else []
    return _entries_from_ids(pending, ids, existing, table_plan=table_plan, run_id=run_id)


# ---------------------------------------------------------------------------
# 整場執行
# ---------------------------------------------------------------------------


def _iter_chunks(rows: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """把 `rows` 這個惰性疊代器切成每份最多 `size` 筆的清單。

    以「每份最多 `batch_size` 筆來源列」為切分單位（而不是「每份
    `batch_size` 筆待寫入列」），是為了讓 `migration_log.jsonl` 的行序能
    在同一個小視窗內重建成與來源列完全相同的順序（見 `run_load` 內的
    合併邏輯），同時把記憶體用量限制在 `batch_size` 筆之內——不必先把
    整張表讀進記憶體才能開始處理。
    """
    chunk: list[dict[str, Any]] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _merge_decisions(
    decisions: list[LogEntry | _PendingRow], pending_entries: list[LogEntry]
) -> list[LogEntry]:
    """把一個 chunk 的決策清單（有的是現成的 `LogEntry`，有的是等著送進
    Odoo 的 `_PendingRow`）跟 `_flush_batch` 回傳的結果依序合併回原本的
    來源列順序。

    `pending_entries` 必須跟 `decisions` 裡 `_PendingRow` 出現的順序完全
    一致（`_flush_batch`／`_flush_individually` 都保證這一點：兩者都是
    依 `pending` 清單的順序逐一 `zip`／`append`，不重新排序）——這裡只是
    照順序把兩條隊伍穿回同一條，不做任何比對或猜測。這是純函式，不碰
    Odoo，純粹測試「合併順序」這個邏輯本身用 (precedent：`_map_connection_
    error` 那類用本地建構的資料結構測純函式，而不是碰網路)。
    """
    pending_iter = iter(pending_entries)
    return [next(pending_iter) if isinstance(d, _PendingRow) else d for d in decisions]


def _missing_source_columns(plans: list[TablePlan], reader: SourceReader) -> list[str]:
    """檢查 `mapping.json` 點名的來源欄位是不是真的存在於來源資料庫。

    `--mapping` 與 `--source` 是兩個獨立的 CLI 參數，沒有任何機制保證
    兩者配對（過期的 mapping、手改過的 mapping、或誤用另一個 case 的
    來源資料庫都會造成這裡對不上）——這是 Task 10 審查修正輪次 1
    Critical C1 的主要防線：在進主迴圈、對任何一列資料動手之前，一次
    比對完所有表的所有欄位，而不是跑到第一列才踩到裸 `KeyError`。

    只比對 `table_plan.columns`（來自 `mapping.json` 的
    `mappings[*].source.column`），不比對 `pk_cols`——後者恆為
    `reader.primary_key(table)` 現場查出來的結果，定義上一定是這張表
    真實存在的欄位，不需要另外驗證。

    回傳缺欄位的完整清單（`"table.column"` 形式，跨所有表），讓呼叫端
    一次講完，不是每次只點名第一個。純函式（`reader.columns()` 是查詢
    但不修改狀態，precedent：這個模組其餘「唯讀查詢」一律不算破壞誠實
    界線，見 `_existing_external_id_names`）。
    """
    missing: list[str] = []
    for table_plan in plans:
        available = list(reader.columns(table_plan.source_table))
        available_set = set(available)
        lowered = {name.lower(): name for name in available}
        for col in table_plan.columns:
            if col.source_column in available_set:
                continue
            # 大小寫提示：T100／Informix → SQLite 的欄位名大小寫漂移，正是
            # 最可能踩到這條的場景（DDL 寫 `Name`、mapping 寫 `name`）。
            # 沒有這個提示的話，訊息會把人導向「mapping 與 source 不是同一次
            # scan 的產物」而看不出真正原因。照抄 `source.py::_check_table`
            # 對「表名」大小寫不符的既有寫法，只是換成欄位名。
            actual = lowered.get(col.source_column.lower())
            hint = f"（大小寫不符：來源資料庫裡是 {actual!r}）" if actual else ""
            missing.append(f"{table_plan.source_table}.{col.source_column}{hint}")
    return missing


@dataclass(frozen=True)
class LoadResult:
    """`run_load` 的回傳值。Task 11／12 依賴這個形狀。"""

    log_path: Path
    entries: tuple[LogEntry, ...]


def run_load(
    mapping: dict[str, Any],
    reader: SourceReader,
    client: OdooClient | None,
    *,
    source_id: str,
    out_dir: Path,
    event_log: EventLog,
    batch_size: int = 100,
    dry_run: bool = False,
    attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> LoadResult:
    """跑一整場載入：`plan_load` 排計畫 → 逐表逐列轉換 → 批次寫進 Odoo →
    `migration_log.jsonl`。

    行為契約（設計 spec §3.5，任務書「行為契約」一節）：

    - `SourceError`／`CyclicReference`／`UnsupportedReference` → 發
      E-4001 並**中止整場**（已經處理過的列仍會留在
      `migration_log.jsonl` 裡，不因為中止而消失）。
    - `TransformError` → 發 E-4002，該筆記 `failed`，**繼續下一筆**。
    - 懸空外鍵（reference 指向的來源記錄已經是 `failed`）→ 記
      `skipped`，**不另外發錯誤碼**。
    - 整批 `load()` 失敗 → 發 E-4004（WARN），降級逐筆以隔離。
    - `RateLimited` → 發 E-4005（WARN），指數退避重試。
    - `dry_run=True` 時 `client` 允許是 `None`——整場走完讀取、轉換、
      排序、組 payload 的全部路徑，但不呼叫 `client` 的任何方法，
      `action` 一律 `skipped`。`dry_run=False` 時 `client` 必須非
      `None`。

    `attempts`／`sleep` 兩個參數是為了讓 `retry_with_backoff` 可測（本機
    測試注入假的 `sleep` 才不會真的睡）；CLI 呼叫端使用預設值
    （`attempts=5`、`sleep=time.sleep`）即可。
    """
    if not dry_run and client is None:
        raise ValueError("client 為 None 時必須 dry_run=True——非 dry-run 一定要有 Odoo 連線")
    if batch_size < 1:
        raise ValueError(f"batch_size 必須 >= 1，收到 {batch_size}")

    writer = _MigrationLogWriter(out_dir / MIGRATION_LOG_FILENAME)
    run_id = event_log.run_id
    entries: list[LogEntry] = []
    # 只累積「因 TransformError 而失敗」與「因上游失敗而被 skip」的
    # external_id（見 `_decide_row` docstring 對範圍的說明）。
    problem_external_ids: set[str] = set()

    try:
        plans = plan_load(mapping, reader, source_id=source_id)
    except (SourceError, CyclicReference, UnsupportedReference) as exc:
        message = f"載入計畫建立失敗，中止整場載入：{exc}"
        event_log.emit("load.extraction_failed", code=E_4001, message=message)
        return LoadResult(log_path=writer.path, entries=())

    try:
        missing_columns = _missing_source_columns(plans, reader)
        if missing_columns:
            message = (
                "mapping.json 提到 "
                + "、".join(missing_columns)
                + "，但來源資料庫沒有這些欄位——mapping 與 source 可能不是同一次 scan 的產物"
            )
            event_log.emit("load.extraction_failed", code=E_4001, message=message)
            return LoadResult(log_path=writer.path, entries=tuple(entries))

        for table_plan in plans:
            pk_cols = reader.primary_key(table_plan.source_table)
            for row_chunk in _iter_chunks(reader.rows(table_plan.source_table), batch_size):
                decisions = [
                    _decide_row(
                        row,
                        table_plan,
                        pk_cols,
                        source_id=source_id,
                        run_id=run_id,
                        dry_run=dry_run,
                        problem_external_ids=problem_external_ids,
                        event_log=event_log,
                    )
                    for row in row_chunk
                ]
                pending = [d for d in decisions if isinstance(d, _PendingRow)]
                pending_entries: list[LogEntry] = []
                if pending:
                    if client is None:
                        # dry_run 已保證 _decide_row 永遠不回傳
                        # _PendingRow（見該函式），所以 pending 到這裡
                        # 一定是空的；client 一定非 None。這一行只是把
                        # 這個不變式寫成看得見的錯誤，而不是靜默地讓
                        # 下面的呼叫噴出令人困惑的 AttributeError。
                        raise AssertionError(
                            "unreachable：dry_run 保證 pending 為空，client 不可能是 None"
                        )
                    pending_entries = _flush_batch(
                        pending,
                        table_plan,
                        client,
                        run_id=run_id,
                        event_log=event_log,
                        attempts=attempts,
                        sleep=sleep,
                    )
                for entry in _merge_decisions(decisions, pending_entries):
                    entries.append(entry)
                    writer.append(entry)
    except SourceError as exc:
        message = f"讀取來源資料時發生錯誤，中止整場載入：{exc}"
        event_log.emit("load.extraction_failed", code=E_4001, message=message)

    return LoadResult(log_path=writer.path, entries=tuple(entries))
