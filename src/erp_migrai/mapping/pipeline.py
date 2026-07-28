"""`map` 階段的完整管線：讀 `inventory.json` → 驗證 → 載入 catalog／rules →
建構 `Mapping` → 寫出 `mapping.json`／`mapping.csv`。

跟 `erp_migrai.inventory.extractor.scan()` 是同一種定位：CLI 與（未來的）
Web UI 是兩個平等的「面板」，業務邏輯——讀檔、驗證、依序呼叫 catalog／
rules／engine／writer、決定哪些例外要往外傳——都住在這裡，不能只存在於
`cli.py`，否則 Web UI 得自己重新兜一遍這七個步驟，且兩邊會慢慢分岔。

`run_map` 對每一種失敗原因都保留各自的例外型別（`InventoryReadError`／
`json.JSONDecodeError`／`InventoryValidationError`／`CatalogError`／
`RuleError`／`erp_migrai.mapping.engine.MapFailed`／
`erp_migrai.mapping.writer.MappingSchemaError`／
`erp_migrai.mapping.writer.MappingSerializationError`／中途寫入失敗的
`OSError`），本模組不吞掉、不轉型、不合併成單一錯誤——呼叫端（CLI）依例外
型別決定怎麼把它轉成使用者看得懂的中文訊息，這正是「CLI 只負責參數解析、
輸出摘要與決定 exit code」這條鐵律劃給呼叫端的那一半工作。

唯一的例外是讀取 `--inventory` 本身失敗：這裡包成 `InventoryReadError`
（而不是讓 `OSError` 原樣往外傳），因為 `run_map` 內部之後還有其他步驟
（`build_mapping` 執行中的 `event_log.emit()`、`write_mapping`／
`write_mapping_csv`）也可能拋出 `OSError`，且語意完全不同（一個是「讀不到
使用者指定的輸入檔」，另一個是「寫不進輸出目錄」）——呼叫端需要能分開接住
這兩種情況、給出點名不同路徑的訊息，光靠 `OSError` 這個型別本身分不出來。

最終整分支審查修正 Important 4：`validate_inventory`（驗證使用者的
`--inventory`）與 `write_mapping` 內部的 `validate_mapping`（驗證本管線自己
產出的 `mapping`）先前都讓 `jsonschema.ValidationError` 原樣往外傳——兩者是
同一個例外型別，`cli.py` 攤平巢狀 `try` 之後，`except ValidationError` 這
一支同時接住了兩種語意完全相反的失敗：前者是「使用者的輸入檔不合規格」，
後者是「引擎本身有缺陷，算出一個連自己 schema 都不符合的 mapping」（例如
`confidence=101`）。兩者混在一起會讓使用者在後一種情況下收到一則指名
`--inventory` 的錯誤訊息，卻去檢查一份完全合法的 inventory，白費工夫。這裡
把 `validate_inventory` 的失敗包成 `InventoryValidationError`（precedent 是
`InventoryReadError`，同一種「包一層讓呼叫端能分開接住」的手法）；
`write_mapping` 內部的失敗則改包成 `erp_migrai.mapping.writer.
MappingSchemaError`，訊息明講「這是映射結果本身的缺陷，不是你的 inventory
的問題」——兩者從此是兩個不同的 Python 型別，`cli.py` 不必再靠訊息內容猜
是哪一種失敗。

**執行順序**（不得調整，`test_cli_map.py` 的必要測試 3 靠這個順序才能通過）：
`write_mapping`／`write_mapping_csv` 一定要在呼叫端檢查 `event_log.has_fatal`
之前完成——即使 `mapping.conflicts` 非空（該階段最終會 exit 1），
`mapping.json`／`mapping.csv` 仍然要先寫出來，讓使用者能直接打開檔案看
衝突內容，而不是必須先解決衝突才看得到任何產出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import ValidationError

from erp_migrai.events import EventLog
from erp_migrai.inventory.extractor import validate_inventory
from erp_migrai.mapping.catalog import OdooModel, load_catalog
from erp_migrai.mapping.engine import build_mapping
from erp_migrai.mapping.models import ConflictKind, Mapping
from erp_migrai.mapping.rules_loader import RuleSet, load_rules
from erp_migrai.mapping.writer import write_mapping, write_mapping_csv


class InventoryReadError(Exception):
    """讀取 `--inventory` 指定的檔案本身失敗（不存在、權限不足等 I/O 問題）。

    訊息裡已經點名路徑與原因，呼叫端只需要原樣印出、不必再自己組訊息。
    """


class InventoryValidationError(Exception):
    """`--inventory` 指定的檔案不符合 `inventory.schema.json`（最終整分支審查
    修正 Important 4）。

    這是真正的使用者輸入問題——precedent 是 `InventoryReadError`：把
    `jsonschema.ValidationError` 包一層，讓呼叫端能用型別分開接住「讀不到
    檔案」「檔案內容不合規格」「引擎自己產出的 mapping 不合規格」這三種不同
    語意的失敗，不必靠例外訊息的文字內容去猜是哪一種。訊息裡已經點名路徑與
    原始的 schema 錯誤，呼叫端只需要原樣印出。
    """


@dataclass
class MapRunResult:
    """`run_map` 的回傳：CLI（以及未來的 Web UI）決定摘要與 exit code 所需的
    一切，一次到位，不必再回頭呼叫任何內部模組。
    """

    mapping: Mapping
    rule_set: RuleSet
    json_path: Path
    csv_path: Path
    # 命中規則、但沒有出現在任何一筆 `mappings`／`conflicts` 裡的規則
    # `origin`（貢獻者鍵名打錯字的典型徵狀，見 `find_unused_column_rules`）。
    unused_rule_origins: list[str]


def find_unused_column_rules(rule_set: RuleSet, mapping: Mapping) -> list[str]:
    """找出 `rule_set.column_rules` 裡「合法但沒有命中任何來源欄位」的規則。

    最終整分支審查修正：一條規則的 `target` 若指到 catalog 裡存在的模型與
    欄位，`load_rules` 會判定它完全合法——但如果規則鍵本身的「來源資料表.
    來源欄位」打錯字（例如 `cust_mast.cust_nol` 但真正的來源欄位是
    `cust_no`），這條規則永遠不會被 `engine._process_column` 查到
    （`rules.column_rules.get((table_name, col_name))` 找不到這個鍵），
    於是真正的欄位落回啟發式評分、規則本身則悄悄地什麼都沒做——沒有任何
    事件、沒有任何 CSV 列點名這件事。

    判準：這條規則的 `(source_table, source_column)` 鍵，若沒有出現在任何
    一筆 `FieldMapping.source`（命中且套用成功）、任何一筆
    `kind == ConflictKind.COLUMN` 的 `MappingConflict`（命中但因為表對到的
    模型與規則衝突而被記成衝突，見 `engine._apply_column_rule`），**或任何
    一筆 `UnmappedColumn`**（最終整分支審查修正 Important 2：命中，但這張表
    本身對不上任何模型，`engine._process_table` 在 `target_model is None`
    時整批欄位改走 `unmapped`，根本不會呼叫 `_process_column`，規則因此
    看起來像沒被用到——但規則鍵本身拼對了，問題出在表層級，不是這條規則），
    就代表這條規則從頭到尾沒有真的被用上。回傳排序過的 `origin`（例如
    `res_partner.yaml#cust_mast.cust_nol`），讓使用者能直接看到自己打錯的
    是哪一個檔案的哪一個鍵。

    controller 重現過的具體反例：兩條規則 `cust_mast.cust_no` 與
    `cust_mast.cust_name` 鍵名都完全正確、但彼此的 `target` 指向不同模型，
    `_infer_model_from_column_rules` 判定這張表本身對不上模型（欄位規則互相
    矛盾），於是這張表所有欄位——包含 `cust_no`／`cust_name` 本身——全部進
    `unmapped`。若不把 `unmapped` 併入 `used_keys`，這兩條拼字完全正確的
    規則會被誤報成「規則鍵名打錯字」，而真正的問題（表級矛盾）已經有自己的
    `MappingConflict`／`E-2001` 在別處回報了，這裡不該重複、且錯誤地再報一次。

    最終整分支審查修正 Minor 6：`rules/` 是外部貢獻者共用的知識庫，會隨著
    專案成長涵蓋愈來愈多資料表——一個 T100 安裝的 inventory 本來就只會含它
    實際擁有的表，某個 T100 安裝的 `cust_mast` 沒有 `cust_credit` 這個欄位是
    完全正常的營運差異，不是規則打錯字。這裡只回傳「規則的來源表**確實存在
    於這份 inventory 裡**」的未命中規則——判準是這個表名有沒有出現在
    `mapping.models`／`mapping.unmapped`／`mapping.conflicts` 任何一筆紀錄裡
    （`engine._process_table` 對每一張有效表都必定會產生其中之一，見
    `engine.py` 模組 docstring 的三分法）。表名完全不存在於這份 inventory 的
    規則，是「預期中的落差」（這個安裝就是沒有那張表），不值得回報；表存在、
    但某個特定欄位的規則鍵沒對上，才是真正「值得檢查」的候選。
    """
    used_keys = {(fm.source.table, fm.source.column) for fm in mapping.mappings}
    used_keys |= {
        (conflict.table, conflict.column)
        for conflict in mapping.conflicts
        if conflict.kind == ConflictKind.COLUMN and conflict.column is not None
    }
    used_keys |= {(u.table, u.column) for u in mapping.unmapped}

    present_tables = {m.source_table for m in mapping.models}
    present_tables |= {u.table for u in mapping.unmapped}
    present_tables |= {c.table for c in mapping.conflicts}

    return [
        rule.origin
        for key, rule in sorted(rule_set.column_rules.items())
        if key not in used_keys and key[0] in present_tables
    ]


def run_map(
    inventory_path: Path,
    out_dir: Path,
    *,
    rules_dir: Path | None,
    catalog_path: Path | None,
    event_log: EventLog,
) -> MapRunResult:
    """跑完整個 `map` 階段：讀 inventory → 驗證 → 載入 catalog／rules →
    `build_mapping` → 寫出 `mapping.json`／`mapping.csv`。

    呼叫端負責在呼叫前準備好 `event_log`（含建立 `out_dir` 與 touch
    `events.jsonl`——那屬於「輸出目錄本身能不能寫」的環境問題，precedent 見
    `cli._fresh_event_log`，不是這條管線的業務邏輯），並在呼叫後依
    `event_log.has_fatal(Stage.MAP, strict=...)` 決定 exit code——那個判斷
    完全不在這裡做，也不該在這裡重新推導哪些錯誤碼算致命。
    """
    try:
        inventory_text = inventory_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InventoryReadError(f"無法讀取 --inventory {inventory_path}：{exc}") from exc

    inventory_data = json.loads(inventory_text)
    try:
        validate_inventory(inventory_data)
    except ValidationError as exc:
        raise InventoryValidationError(
            f"--inventory {inventory_path} 不符合 inventory.schema.json：{exc.message}"
        ) from exc

    catalog_data: dict[str, OdooModel] = load_catalog(catalog_path)
    rule_set = load_rules(catalog_data, rules_dir)

    mapping = build_mapping(inventory_data, catalog_data, rule_set, event_log=event_log)

    # ruling 3：即使之後 event_log.has_fatal(...) 會判定這次執行致命
    # （例如 mapping.conflicts 非空觸發 E-2001），mapping.json／mapping.csv
    # 仍然要先寫出來——順序不得調整，呼叫端才能在「exit 1」的同時仍然看到
    # 衝突內容本身，而不是必須先解決衝突才看得到任何檔案。
    json_path = write_mapping(mapping, out_dir)
    csv_path = write_mapping_csv(mapping, out_dir)

    unused_rule_origins = find_unused_column_rules(rule_set, mapping)

    return MapRunResult(
        mapping=mapping,
        rule_set=rule_set,
        json_path=json_path,
        csv_path=csv_path,
        unused_rule_origins=unused_rule_origins,
    )
