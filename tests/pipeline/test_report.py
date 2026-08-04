"""`report.py` 的測試：`build_report`（純函式）與 `render_markdown`
（`.md` 完全由 `.json` 渲染，純函式）。
"""

from __future__ import annotations

import pytest

from erp_migrai.pipeline.report import (
    ReportSchemaError,
    build_report,
    render_markdown,
    validate_report,
)
from erp_migrai.pipeline.verify import Check

# --------------------------------------------------------------------------
# build_report
# --------------------------------------------------------------------------


def test_build_report_shape():
    """`source_count`／`target_count` 刻意用非對稱的 7／3（不是兩個相等的
    3／3）——審查修正輪次 1 Critical C3：若只用對稱夾具，`build_report`
    把兩個欄位對調（M47）不會被這條測試發現，因為對調後 dict 長得一模
    一樣。這裡的完整 dict 相等比較能直接分辨兩者。
    """
    checks = [Check("row_count", "cust_mast", "res.partner", "pass", 7, 3, None, None)]
    report = build_report(checks, source_id="case03", synthetic=True)
    assert report["schema_version"] == "1.0"
    assert report["source_id"] == "case03"
    assert report["synthetic"] is True
    assert report["checks"] == [
        {
            "kind": "row_count",
            "source_table": "cust_mast",
            "target_model": "res.partner",
            "status": "pass",
            "source_count": 7,
            "target_count": 3,
            "source_amount": None,
            "target_amount": None,
        }
    ]
    assert report["summary"] == {"total": 1, "passed": 1, "failed": 0}


def test_build_report_empty_checks_list():
    """`checks` 是空清單時，summary 三個欄位都該是 0——不是拋例外，也不是
    某個欄位缺席（precedent：schema 的 `test_schema_accepts_empty_checks_
    array`，這裡驗證 `build_report` 本身真的產出這個形狀）。"""
    report = build_report([], source_id="case03", synthetic=True)
    assert report["checks"] == []
    assert report["summary"] == {"total": 0, "passed": 0, "failed": 0}


def test_build_report_synthetic_false_is_preserved():
    """`synthetic` 一路從 inventory 傳下來，不能在中途消失或被悄悄轉型
    （例如被 `bool()` 以外的方式擠壓成永遠是 `True`）。"""
    report = build_report([], source_id="case03", synthetic=False)
    assert report["synthetic"] is False


# --------------------------------------------------------------------------
# render_markdown：斷言 .md 的每一個數字都來自 .json
# --------------------------------------------------------------------------


def test_render_markdown_contains_table_names_and_summary_numbers():
    """給一份含 1 pass 1 fail 的報告，斷言渲染結果同時含兩個表名、含
    summary 的數字，且失敗那一列的完整表格列（含 `source_count`／
    `target_count` 依序出現的實際數值）必須原樣出現。

    審查修正輪次 1 Critical C3：原本只斷言 `"5" in md` 跟 `"4" in md`——
    這種寫法對『兩個數字對調』（M18：md 的兩欄對調）完全免疫，因為對調
    後兩個數字字串一樣都還在文字裡的某處。這裡改斷言整列字串，數字的
    先後順序才真的被釘住。
    """
    checks = [
        Check("row_count", "cust_mast", "res.partner", "pass", 3, 3, None, None),
        Check("row_count", "ord_mast", "sale.order", "fail", 5, 4, None, None),
    ]
    report = build_report(checks, source_id="case03", synthetic=True)
    md = render_markdown(report)

    assert "cust_mast" in md
    # summary：總計 2、失敗 1
    assert "總計：2" in md
    assert "失敗：1" in md
    # 失敗那一列必須完整依序出現「5」（source）與「4」（target），不能只
    # 是隨便哪裡出現這兩個字元。
    assert "| row_count | ord_mast | sale.order | ❌ fail | 5 | 4 |" in md


def test_render_markdown_amount_check_shows_amount_strings_not_counts():
    checks = [
        Check(
            "amount_subtotal", "ord_deta", "sale.order.line", "fail", None, None, "100.00", "99.00"
        )
    ]
    report = build_report(checks, source_id="case03", synthetic=True)
    md = render_markdown(report)
    assert "100.00" in md
    assert "99.00" in md


def test_render_markdown_does_not_recompute_summary():
    """`.md` 完全由 `.json` 渲染：直接餵一份手工構造、summary 與
    checks 內容『不一致』的 dict（正常情況下不會發生，但這條測試藉此證明
    `render_markdown` 真的只是讀 `report['summary']`，不是自己重新從
    `checks` 數一次）。"""
    report = {
        "schema_version": "1.0",
        "source_id": "case03",
        "synthetic": True,
        "checks": [],
        "summary": {"total": 999, "passed": 999, "failed": 0},
    }
    md = render_markdown(report)
    assert "總計：999" in md
    assert "通過：999" in md


def test_render_markdown_includes_source_id_and_synthetic():
    report = build_report([], source_id="case03", synthetic=True)
    md = render_markdown(report)
    assert "case03" in md
    assert "True" in md


def test_render_markdown_pass_and_fail_have_different_symbols():
    """人要能一眼分辨 pass／fail，不是只能靠讀英文字。"""
    checks = [
        Check("row_count", "a", "m", "pass", 1, 1, None, None),
        Check("row_count", "b", "n", "fail", 2, 1, None, None),
    ]
    report = build_report(checks, source_id="c3", synthetic=True)
    md = render_markdown(report)
    assert "✅" in md
    assert "❌" in md


# --------------------------------------------------------------------------
# validate_report：build_report 真正的產出必須通過
# verification_report.schema.json（審查修正輪次 1 Critical C4）
# --------------------------------------------------------------------------


def test_build_report_output_of_row_count_and_amount_checks_passes_schema_validation():
    """`test_verification_report_schema.py` 的 36 條測試驗的是一份手寫
    夾具，從沒驗過 `build_report()` 真正的產出——這裡直接把 `build_report`
    的輸出丟進 `validate_report`，`build_report` 若不小心在某個 kind 多吐
    一個 schema 禁止的鍵（M58），這裡會直接爆炸而不是靜靜地全綠。
    """
    checks = [
        Check("row_count", "cust_mast", "res.partner", "pass", 3, 3, None, None),
        Check(
            "amount_subtotal",
            "ord_deta",
            "sale.order.line",
            "pass",
            None,
            None,
            "26999.99",
            "26999.99",
        ),
    ]
    report = build_report(checks, source_id="case03", synthetic=True)
    validate_report(report)  # 不拋例外就是通過


def test_build_report_output_with_empty_checks_passes_schema_validation():
    report = build_report([], source_id="case03", synthetic=False)
    validate_report(report)


def test_validate_report_rejects_extra_key_on_a_check():
    """模擬 M58：`build_report` 若在某個 check 多吐一個 schema
    `additionalProperties: false` 禁止的鍵，`validate_report` 必須拋
    `ReportSchemaError`，不能安靜地放行。
    """
    report = build_report(
        [Check("row_count", "a", "m", "pass", 1, 1, None, None)], source_id="c3", synthetic=True
    )
    report["checks"][0]["note"] = "schema 不允許的額外鍵"
    with pytest.raises(ReportSchemaError):
        validate_report(report)


def test_validate_report_rejects_report_missing_required_field():
    report = build_report([], source_id="c3", synthetic=True)
    del report["schema_version"]
    with pytest.raises(ReportSchemaError):
        validate_report(report)
