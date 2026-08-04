"""`verification_report.schema.json` 的行為契約。

跟 `test_migration_log_schema.py`（Task 10 Step 1）刻意保持相同的結構：
正向夾具、每個約束一條負例、對兩個 enum（`kind`／`status`）各自斷言完整
值集合。schema 驗證的是**最終要寫進 `verification_report.json` 的 dict**
（`cli.verify_command` 寫出前一定會呼叫 `json.dumps`，precedent：
`loader.py` 對 `migration_log.schema.json` 的關係——但這裡是 `report.
build_report` 產出的 dict 本身就該直接符合 schema，不需要另一層轉換）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from erp_migrai.pipeline.verify import KINDS, STATUSES

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "erp_migrai"
    / "pipeline"
    / "schema"
    / "verification_report.schema.json"
)
_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)


def _row_count_check(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "row_count",
        "source_table": "cust_mast",
        "target_model": "res.partner",
        "status": "pass",
        "source_count": 3,
        "target_count": 3,
        "source_amount": None,
        "target_amount": None,
    }
    base.update(overrides)
    return base


def _amount_check(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "amount_subtotal",
        "source_table": "ord_deta",
        "target_model": "sale.order.line",
        "status": "pass",
        "source_count": None,
        "target_count": None,
        "source_amount": "26999.99",
        "target_amount": "26999.99",
    }
    base.update(overrides)
    return base


def _report(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": "1.0",
        "source_id": "case03",
        "synthetic": True,
        "checks": [_row_count_check(), _amount_check()],
        "summary": {"total": 2, "passed": 2, "failed": 0},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# 正向：一份含 row_count 與 amount_subtotal 各一筆的合法報告
# --------------------------------------------------------------------------


def test_valid_report_passes_schema():
    _VALIDATOR.validate(_report())  # 不拋例外就是通過


@pytest.mark.parametrize("status", ["pass", "fail"])
def test_valid_check_for_each_status_passes_schema(status):
    _VALIDATOR.validate(_report(checks=[_row_count_check(status=status)]))


# --------------------------------------------------------------------------
# kind／status 的 enum：完整值集合斷言（原則 5.4；precedent：
# test_schema_action_enum_is_exactly_the_four_values）
# --------------------------------------------------------------------------


def test_schema_kind_enum_is_exactly_the_two_values():
    kind_schema = _SCHEMA["properties"]["checks"]["items"]["properties"]["kind"]
    assert set(kind_schema["enum"]) == KINDS


def test_schema_status_enum_is_exactly_the_two_values():
    status_schema = _SCHEMA["properties"]["checks"]["items"]["properties"]["status"]
    assert set(status_schema["enum"]) == STATUSES


def test_kinds_constant_has_exactly_the_two_documented_values():
    assert {"row_count", "amount_subtotal"} == KINDS


def test_statuses_constant_has_exactly_the_two_documented_values():
    assert {"pass", "fail"} == STATUSES


def test_schema_rejects_a_third_kind_value():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_report(checks=[_row_count_check(kind="balance")]))


def test_schema_rejects_a_third_status_value():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_report(checks=[_row_count_check(status="warn")]))


# --------------------------------------------------------------------------
# additionalProperties: false（頂層與 checks[] 兩層都要守）
# --------------------------------------------------------------------------


def test_schema_rejects_unknown_top_level_property():
    report = _report()
    report["timestamp"] = "2026-01-01T00:00:00Z"
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(report)


def test_schema_rejects_unknown_check_property():
    check = _row_count_check()
    check["note"] = "多出來的欄位"
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_report(checks=[check]))


# --------------------------------------------------------------------------
# 頂層每個必填欄位缺席各一條負例夾具
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["schema_version", "source_id", "synthetic", "checks", "summary"])
def test_schema_rejects_missing_top_level_required_field(field):
    report = _report()
    del report[field]
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(report)


@pytest.mark.parametrize(
    "field",
    [
        "kind",
        "source_table",
        "target_model",
        "status",
        "source_count",
        "target_count",
        "source_amount",
        "target_amount",
    ],
)
def test_schema_rejects_missing_check_required_field(field):
    check = _row_count_check()
    del check[field]
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_report(checks=[check]))


@pytest.mark.parametrize("field", ["total", "passed", "failed"])
def test_schema_rejects_missing_summary_field(field):
    report = _report()
    del report["summary"][field]
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(report)


# --------------------------------------------------------------------------
# 型別：source_count／target_count 是 ["integer","null"]，
# source_amount／target_amount 是 ["string","null"]
# --------------------------------------------------------------------------


def test_schema_accepts_null_counts_for_amount_check():
    _VALIDATOR.validate(_report(checks=[_amount_check()]))


def test_schema_accepts_null_amounts_for_row_count_check():
    _VALIDATOR.validate(_report(checks=[_row_count_check()]))


def test_schema_rejects_string_source_count():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_report(checks=[_row_count_check(source_count="3")]))


def test_schema_rejects_float_target_count():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_report(checks=[_row_count_check(target_count=3.5)]))


def test_schema_rejects_numeric_source_amount():
    """金額必須是字串（Decimal 的字面表示），絕不是 JSON 數字——
    浮點數表示金額正是本任務要擋的地雷（見 verify.py 模組 docstring）。
    """
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_report(checks=[_amount_check(source_amount=26999.99)]))


def test_schema_accepts_null_source_amount_for_row_count_check():
    _VALIDATOR.validate(_report(checks=[_row_count_check(source_amount=None)]))


# --------------------------------------------------------------------------
# summary 三個欄位是 integer
# --------------------------------------------------------------------------


def test_schema_rejects_non_integer_summary_field():
    report = _report()
    report["summary"]["total"] = "2"
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(report)


# --------------------------------------------------------------------------
# synthetic 是 boolean
# --------------------------------------------------------------------------


def test_schema_rejects_non_boolean_synthetic():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_report(synthetic="true"))


# --------------------------------------------------------------------------
# checks 可以是空陣列（沒有任何表要驗）
# --------------------------------------------------------------------------


def test_schema_accepts_empty_checks_array():
    _VALIDATOR.validate(_report(checks=[], summary={"total": 0, "passed": 0, "failed": 0}))
