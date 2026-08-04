"""`verification_report.json` 的建構，以及 `.md` 的渲染。純函式。

P3-3（設計 spec）：`verification_report.json` 是唯一事實來源，`.md`
**完全由它渲染**——測試斷言 JSON，不用正規表示式抓 markdown（後者極容易
變成「什麼都沒檢查到卻亮綠燈」的空守衛）。`render_markdown` 因此只讀
`build_report` 產出的（或從磁碟讀回的同形狀）dict，不重新計算任何數字。
"""

from __future__ import annotations

from typing import Any

from erp_migrai.pipeline.verify import Check

SCHEMA_VERSION = "1.0"


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
