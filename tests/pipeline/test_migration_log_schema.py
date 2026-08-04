"""`migration_log.schema.json` 的行為契約，與 `LogEntry.__post_init__` 的
建構期不變式。

兩層分工：schema 驗證的是**最終要寫進 `migration_log.jsonl` 的 dict**
（`loader.py` 寫出前一定會呼叫，precedent：`mapping/writer.py` 對
`mapping.schema.json` 的關係）；`LogEntry.__post_init__` 則在**物件建構
當下**就把同一份不變式（action 與 odoo_id／error_code 的互斥組合）擋下，
是更早、更好定位的第二道防線。這裡兩邊都測，因為兩邊都可能被改壞而彼此
互不知情。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from erp_migrai.pipeline.models import ACTIONS, LogEntry

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "erp_migrai"
    / "pipeline"
    / "schema"
    / "migration_log.schema.json"
)
_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)


def _valid_entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "source_table": "cust_mast",
        "source_pk": ["C001"],
        "external_id": "migrai.case03.cust_mast.C001",
        "target_model": "res.partner",
        "action": "created",
        "odoo_id": 42,
        "error_code": None,
        "message": "",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# 正向：四種 action 各一條合法夾具
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "odoo_id", "error_code"),
    [
        ("created", 1, None),
        ("updated", 2, None),
        ("failed", None, "E-4002"),
        ("skipped", None, None),
    ],
)
def test_valid_entry_for_each_action_passes_schema(action, odoo_id, error_code):
    entry = _valid_entry(action=action, odoo_id=odoo_id, error_code=error_code)
    _VALIDATOR.validate(entry)  # 不拋例外就是通過


# --------------------------------------------------------------------------
# action 的 enum：完整值集合斷言（原則 5.4；precedent：
# test_conflict_kinds_are_exactly_three）
# --------------------------------------------------------------------------


def test_schema_action_enum_is_exactly_the_four_values():
    assert set(_SCHEMA["properties"]["action"]["enum"]) == ACTIONS


def test_actions_constant_has_exactly_the_four_documented_values():
    assert {"created", "updated", "failed", "skipped"} == ACTIONS


def test_schema_rejects_a_fifth_action_value():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_valid_entry(action="archived", odoo_id=None, error_code=None))


# --------------------------------------------------------------------------
# additionalProperties: false
# --------------------------------------------------------------------------


def test_schema_rejects_unknown_property():
    entry = _valid_entry()
    entry["timestamp"] = "2026-01-01T00:00:00Z"
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(entry)


# --------------------------------------------------------------------------
# 每個必填欄位缺席各一條負例夾具
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "source_table",
        "source_pk",
        "external_id",
        "target_model",
        "action",
        "odoo_id",
        "error_code",
        "message",
    ],
)
def test_schema_rejects_missing_required_field(field):
    entry = _valid_entry()
    del entry[field]
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(entry)


# --------------------------------------------------------------------------
# source_pk：array of string, minItems 1
# --------------------------------------------------------------------------


def test_schema_rejects_empty_source_pk():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_valid_entry(source_pk=[]))


def test_schema_rejects_non_string_source_pk_item():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_valid_entry(source_pk=[1]))


def test_schema_accepts_composite_source_pk():
    _VALIDATOR.validate(_valid_entry(source_pk=["SO001", "1"]))


# --------------------------------------------------------------------------
# odoo_id：["integer", "null"]
# --------------------------------------------------------------------------


def test_schema_accepts_null_odoo_id():
    _VALIDATOR.validate(_valid_entry(action="skipped", odoo_id=None, error_code=None))


def test_schema_rejects_string_odoo_id():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_valid_entry(odoo_id="42"))


def test_schema_rejects_float_odoo_id():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_valid_entry(odoo_id=42.5))


# --------------------------------------------------------------------------
# error_code：["string", "null"]
# --------------------------------------------------------------------------


def test_schema_rejects_integer_error_code():
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(_valid_entry(action="failed", odoo_id=None, error_code=4002))


def test_schema_accepts_null_error_code():
    _VALIDATOR.validate(_valid_entry(action="created", odoo_id=1, error_code=None))


# --------------------------------------------------------------------------
# LogEntry.__post_init__：建構期的第二道防線
# --------------------------------------------------------------------------


def _entry(**overrides: object) -> LogEntry:
    base = {
        "run_id": "r",
        "source_table": "t",
        "source_pk": ("1",),
        "external_id": "migrai.c3.t.1",
        "target_model": "m",
        "action": "created",
        "odoo_id": 1,
        "error_code": None,
        "message": "",
    }
    base.update(overrides)
    return LogEntry(**base)


def test_log_entry_rejects_unknown_action():
    with pytest.raises(ValueError):
        _entry(action="archived", odoo_id=None)


def test_log_entry_created_requires_odoo_id():
    with pytest.raises(ValueError):
        _entry(action="created", odoo_id=None)


def test_log_entry_created_rejects_error_code():
    with pytest.raises(ValueError):
        _entry(action="created", odoo_id=1, error_code="E-4002")


def test_log_entry_failed_requires_error_code():
    with pytest.raises(ValueError):
        _entry(action="failed", odoo_id=None, error_code=None, message="boom")


def test_log_entry_failed_rejects_odoo_id():
    with pytest.raises(ValueError):
        _entry(action="failed", odoo_id=1, error_code="E-4002")


def test_log_entry_skipped_forbids_error_code():
    with pytest.raises(ValueError):
        _entry(action="skipped", odoo_id=None, error_code="E-4002")


def test_log_entry_skipped_forbids_odoo_id():
    with pytest.raises(ValueError):
        _entry(action="skipped", odoo_id=1, error_code=None)


def test_log_entry_rejects_empty_source_pk():
    with pytest.raises(ValueError):
        _entry(source_pk=())


def test_log_entry_accepts_each_action_with_correct_companion_fields():
    """反向對照：四種合法組合都不該拋例外——確保上面四條負例不是巧合地
    永遠拋錯（例如 `__post_init__` 整個壞掉、對任何輸入都拋）。"""
    _entry(action="created", odoo_id=1, error_code=None)
    _entry(action="updated", odoo_id=2, error_code=None)
    _entry(action="failed", odoo_id=None, error_code="E-4002", message="x")
    _entry(action="skipped", odoo_id=None, error_code=None)


def test_log_entry_to_dict_matches_schema_shape():
    entry = LogEntry(
        run_id="r",
        source_table="ord_deta",
        source_pk=("SO001", "1"),
        external_id="migrai.c3.ord_deta.SO001-1",
        target_model="sale.order.line",
        action="created",
        odoo_id=99,
        error_code=None,
        message="",
    )
    payload = entry.to_dict()
    _VALIDATOR.validate(payload)
    assert payload["source_pk"] == ["SO001", "1"]
    assert isinstance(payload["source_pk"], list)
