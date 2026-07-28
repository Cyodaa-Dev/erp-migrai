"""錯誤碼與事件記錄的行為。"""

import json
from datetime import UTC, datetime

import pytest

from erp_migrai.events import (
    E_1001,
    E_1002,
    E_4004,
    ERROR_CODES,
    ErrorCode,
    EventLog,
    Severity,
    Stage,
)


@pytest.fixture
def log(tmp_path):
    """注入固定的時鐘與 id 產生器，讓事件內容可預期。"""
    return EventLog(
        path=tmp_path / "events.jsonl",
        run_id="run-fixed",
        clock=lambda: datetime(2026, 7, 26, 1, 2, 3, tzinfo=UTC),
        id_factory=lambda: "id-fixed",
    )


def test_error_code_table_contains_exactly_the_mvp_eleven():
    assert sorted(ERROR_CODES) == [
        "E-1001",
        "E-1002",
        "E-2001",
        "E-2002",
        "E-4001",
        "E-4002",
        "E-4003",
        "E-4004",
        "E-4005",
        "E-5001",
        "E-5002",
    ]


def test_module_four_codes_are_absent():
    for code in ("E-3001", "E-3002", "E-3003", "E-1003"):
        assert code not in ERROR_CODES


def test_emit_writes_one_jsonl_line_with_expected_fields(log):
    log.emit("INVENTORY_SCAN_STARTED", message="開始掃描")

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "event_id": "id-fixed",
        "event_type": "INVENTORY_SCAN_STARTED",
        "timestamp": "2026-07-26T01:02:03Z",
        "severity": "INFO",
        "error_code": None,
        "message": "開始掃描",
        "details": {},
        "run_id": "run-fixed",
    }


def test_severity_comes_from_the_error_code_when_one_is_given(log):
    event = log.emit("INVENTORY_PARSE_ERROR", code=E_1002, message="不支援")
    assert event["severity"] == "ERROR"
    assert event["error_code"] == "E-1002"


def test_e1002_is_not_fatal_for_scan(log):
    """不支援語法是 scan 的產出，不是 scan 的失敗。"""
    log.emit("INVENTORY_PARSE_ERROR", code=E_1002)
    assert log.has_fatal(Stage.SCAN) is False


def test_e1002_becomes_fatal_under_strict(log):
    log.emit("INVENTORY_PARSE_ERROR", code=E_1002)
    assert log.has_fatal(Stage.SCAN, strict=True) is True


def test_e1001_is_always_fatal_for_scan(log):
    log.emit("INVENTORY_SCAN_FAILED", code=E_1001)
    assert log.has_fatal(Stage.SCAN) is True


def test_events_are_appended_not_overwritten(log):
    log.emit("A")
    log.emit("B")
    assert len(log.path.read_text(encoding="utf-8").splitlines()) == 2
    assert [e["event_type"] for e in log.events] == ["A", "B"]


def test_non_ascii_is_written_unescaped(log):
    log.emit("INVENTORY_SCAN_STARTED", message="開始掃描")
    assert "開始掃描" in log.path.read_text(encoding="utf-8")


def test_emit_rejects_unknown_error_code_string(log):
    """emit() 應拒絕表碼不在規範表內的 ErrorCode。"""
    unknown_code = ErrorCode("E-9999", "UNKNOWN", Severity.ERROR, Stage.SCAN)
    with pytest.raises(ValueError, match="E-9999"):
        log.emit("TEST_EVENT", code=unknown_code)


def test_emit_rejects_mismatched_error_code_fields(log):
    """emit() 應拒絕表碼在表內但欄位不符的 ErrorCode。"""
    # E-1002 的正式登記項目是 ErrorCode("E-1002", "PARSE_ERROR", Severity.ERROR, Stage.SCAN)
    # 這裡刻意讓 severity 欄位的值跟正式表不符
    mismatched_code = ErrorCode("E-1002", "PARSE_ERROR", Severity.WARN, Stage.SCAN)
    with pytest.raises(ValueError, match="E-1002"):
        log.emit("TEST_EVENT", code=mismatched_code)


def test_emit_accepts_all_eleven_canonical_error_codes(log):
    """emit() 應接受所有 11 個規範的錯誤碼常數。"""
    canonical_codes = [
        "E_1001",
        "E_1002",
        "E_2001",
        "E_2002",
        "E_4001",
        "E_4002",
        "E_4003",
        "E_4004",
        "E_4005",
        "E_5001",
        "E_5002",
    ]
    import erp_migrai.events as events_module

    for code_name in canonical_codes:
        code = getattr(events_module, code_name)
        # 不應拋出例外
        log.emit("TEST_EVENT", code=code)
    # 驗證全部都已被記錄
    assert len(log.events) == 11


def test_warn_severity_stays_non_fatal_under_strict(log):
    """E_4004 (WARN severity) 在 strict=True 時仍應非致命。"""
    log.emit("BATCH_RETRY_EVENT", code=E_4004)
    assert log.has_fatal(Stage.LOAD, strict=True) is False
