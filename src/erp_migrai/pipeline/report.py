"""`verification_report.json` 的建構，以及 `.md` 的渲染。純函式。

P3-3（設計 spec）：`verification_report.json` 是唯一事實來源，`.md`
**完全由它渲染**——測試斷言 JSON，不用正規表示式抓 markdown（後者極容易
變成「什麼都沒檢查到卻亮綠燈」的空守衛）。`render_markdown` 因此只讀
`build_report` 產出的（或從磁碟讀回的同形狀）dict，不重新計算任何數字。

## `validate_report`：寫出前的最後一道防線

審查修正輪次 1 Critical C4：`verification_report.schema.json` 原本完全
沒有接線——runtime 從不驗證，`test_verification_report_schema.py` 的 36
條測試驗的是一份手寫夾具，不是 `build_report()` 真正的產出，`build_report`
若不小心在某個 kind 多吐一個 schema 禁止的鍵，不會有任何測試發現。
`validate_report` 補上這一關（precedent：`loader._validate_and_dump`
對 `migration_log.schema.json` 的關係），`cli.verify_command` 在寫檔前
呼叫它。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from erp_migrai.pipeline.verify import Check

SCHEMA_VERSION = "1.0"

_SCHEMA_PATH = Path(__file__).parent / "schema" / "verification_report.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)


class ReportSchemaError(Exception):
    """`build_report` 組出的內容不符合 `verification_report.schema.json`。

    這是 `report.py` 自己的缺陷，不是使用者輸入的問題（precedent：
    `loader.LogEntrySchemaError`）——`build_report` 的輸入是型別已知的
    `list[Check]`，不會有「使用者餵了壞資料」這種情境。
    """


def build_report(checks: list[Check], *, source_id: str, synthetic: bool) -> dict[str, Any]:
    """把一串 `Check` 組成 `verification_report.json` 的完整內容。

    🔴 `summary` 必須從 `checks` 數出來，不另外累加計數器（原則 3）：
    覆蓋率型統計若靠另一個計數器維護，`checks` 少一項時分母不會跟著變，
    報告會朝樂觀方向說謊——這正是計劃 2 那個 Critical 缺陷的形狀。這裡
    `total`／`passed`／`failed` 三個數字全部從 `len(checks)` 與逐一檢查
    `c.status` 現場算出，沒有任何獨立維護的計數器。

    `source_id`／`synthetic` 是呼叫端（`cli.py`）已知的字串／布林值，
    這個函式不驗證它們的內容（例如不檢查 `source_id` 是否真的存在於
    `checks` 裡的某個 external_id）——那是更上層的關注點。
    """
    total = len(checks)
    passed = sum(1 for c in checks if c.status == "pass")
    failed = total - passed
    return {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "synthetic": synthetic,
        "checks": [
            {
                "kind": c.kind,
                "source_table": c.source_table,
                "target_model": c.target_model,
                "status": c.status,
                "source_count": c.source_count,
                "target_count": c.target_count,
                "source_amount": c.source_amount,
                "target_amount": c.target_amount,
            }
            for c in checks
        ],
        "summary": {"total": total, "passed": passed, "failed": failed},
    }


def validate_report(report: dict[str, Any]) -> None:
    """驗證 `report`（`build_report` 的輸出）符合
    `verification_report.schema.json`；不合法時拋 `ReportSchemaError`。

    `cli.verify_command` 在把 `report` 寫成 JSON／md 之前呼叫這個函式
    （precedent：`loader._validate_and_dump` 在寫出每一筆 `migration_log`
    記錄之前先驗證）。純函式：只驗證、不寫檔，回傳 `None` 代表通過。
    """
    try:
        _VALIDATOR.validate(report)
    except ValidationError as exc:
        raise ReportSchemaError(
            "build_report 組出的 verification_report 不符合 "
            f"verification_report.schema.json（這是 report.py 的缺陷，"
            f"不是你的輸入有問題，請回報 issue）：{exc.message}"
        ) from exc


def render_markdown(report: dict[str, Any]) -> str:
    """把 `report`（`build_report` 的輸出，或從 `verification_report.json`
    讀回的同形狀 dict）渲染成 markdown。

    `.md` 完全由 `.json` 渲染：這裡不重新計算 `summary`、不重新判斷
    `status`，只是把已經算好的內容格式化成表格。失敗那一列的 `source_count`
    ／`target_count`（或 `source_amount`／`target_amount`）兩個實際數值都
    直接放進表格，人要能從報告看出差了幾筆／差了多少錢，而不是只看到
    「失敗」兩個字。
    """
    lines = [
        "# ERP MIGRAI 驗證報告",
        "",
        f"- source_id: {report['source_id']}",
        f"- synthetic: {report['synthetic']}",
        f"- schema_version: {report['schema_version']}",
        "",
        "## 摘要",
        "",
        f"- 總計：{report['summary']['total']}",
        f"- 通過：{report['summary']['passed']}",
        f"- 失敗：{report['summary']['failed']}",
        "",
        "## 檢查明細",
        "",
        "| kind | source_table | target_model | status | source | target |",
        "|---|---|---|---|---|---|",
    ]
    for check in report["checks"]:
        if check["kind"] == "row_count":
            source = str(check["source_count"])
            target = str(check["target_count"])
        else:
            source = str(check["source_amount"])
            target = str(check["target_amount"])
        symbol = "✅" if check["status"] == "pass" else "❌"
        lines.append(
            f"| {check['kind']} | {check['source_table']} | {check['target_model']} | "
            f"{symbol} {check['status']} | {source} | {target} |"
        )
    lines.append("")
    return "\n".join(lines)
