"""`report.py` 的測試：`build_report`（純函式）與 `render_markdown`
（`.md` 完全由 `.json` 渲染，純函式）。
"""

from __future__ import annotations

from erp_migrai.pipeline.report import build_report, render_markdown
from erp_migrai.pipeline.verify import Check

# --------------------------------------------------------------------------
# build_report
# --------------------------------------------------------------------------


def test_build_report_shape():
    checks = [Check("row_count", "cust_mast", "res.partner", "pass", 3, 3, None, None)]
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
            "source_count": 3,
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
    """給一份含 1 pass 1 fail 的報告，斷言渲染結果同時含兩個表名、含 `1`
    與 `2` 這兩個數字，且含失敗那一列的 `source_count`／`target_count`
    兩個實際數值（人要能從報告直接看出差了幾筆，而不是只看到「失敗」
    兩個字）。"""
    checks = [
        Check("row_count", "cust_mast", "res.partner", "pass", 3, 3, None, None),
        Check("row_count", "ord_mast", "sale.order", "fail", 5, 4, None, None),
    ]
    report = build_report(checks, source_id="case03", synthetic=True)
    md = render_markdown(report)

    assert "cust_mast" in md
    assert "ord_mast" in md
    # summary：總計 2、失敗 1
    assert "總計：2" in md
    assert "失敗：1" in md
    # 失敗那一列的實際數字（5 vs 4）必須出現，不能只寫「失敗」兩個字。
    assert "5" in md
    assert "4" in md


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
