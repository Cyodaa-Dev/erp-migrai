"""`pipeline/loader.py` 的行為契約。

⚠️ **誠實界線**（Global Constraints 第 2 條、任務書「誠實界線」一節）：這裡
只測「不需要 Odoo 存在」的部分——`retry_with_backoff`（注入普通函式與
`sleep`）、`migration_log.jsonl` 的寫出與 schema 驗證、`_decide_row` 的
本地決策邏輯（轉換、external_id、懸空外鍵偵測、dry-run）、`run_load` 在
`--dry-run` 下的端到端行為、`E-4001` 中止路徑。

**刻意不測**`_flush_batch`／`_flush_individually`／`_existing_external_id_
names`——這幾個函式的存在理由就是「呼叫 `client.execute_kw()`」，要測
它們的行為就必須餵一個模擬 Odoo `load()`／`ir.model.data.search_read()`
回應形狀的替身物件，那正是「假的 Odoo client」（任務書 Global Constraints
第 2 條明文禁止；precedent：`odoo_client.py` 的 `_send()`／`authenticate()`
／`execute_kw()` 整個網路往返同樣沒有本機單元測試）。這幾個函式的正確性
一律留給 Task 12 對真 Odoo 的 CI 整合測試驗證，清單見報告。

`_DictSourceReader` 不是假的 Odoo——它是 `SourceReader` 這個介面（`plan_
load`／`run_load` 唯一依賴的來源資料抽象）的測試替身，precedent：
`tests/pipeline/test_planner.py` 的 `_FixedPkReader` already 立過這條界線
（`SourceReader` 是被測系統自己的介面，不是外部系統 Odoo）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from erp_migrai.events import EventLog
from erp_migrai.pipeline.loader import (
    LogEntrySchemaError,
    _decide_row,
    _first_error_message,
    _iter_chunks,
    _merge_decisions,
    _MigrationLogWriter,
    _name_without_module_prefix,
    _PendingRow,
    _validate_and_dump,
    retry_with_backoff,
    run_load,
)
from erp_migrai.pipeline.models import LogEntry
from erp_migrai.pipeline.odoo_client import OdooError, RateLimited
from erp_migrai.pipeline.planner import CyclicReference, plan_load
from erp_migrai.pipeline.source import SourceError

# --------------------------------------------------------------------------
# 測試替身：SourceReader（不是 Odoo，見模組 docstring）
# --------------------------------------------------------------------------


class _DictSourceReader:
    """純記憶體的 `SourceReader`：資料直接來自建構時傳入的 dict，不碰
    SQLite，也完全不知道 Odoo 存在。"""

    def __init__(self, tables: dict[str, list[dict[str, Any]]], pk: dict[str, list[str]]) -> None:
        self._tables = tables
        self._pk = pk

    def tables(self) -> list[str]:
        return sorted(self._tables)

    def primary_key(self, table: str) -> list[str]:
        return list(self._pk.get(table, []))

    def columns(self, table: str) -> list[str]:
        rows = self._tables.get(table, [])
        return list(rows[0].keys()) if rows else []

    def rows(self, table: str) -> Iterator[dict[str, Any]]:
        yield from self._tables.get(table, [])

    def count(self, table: str) -> int:
        return len(self._tables.get(table, []))


class _FlakyReader(_DictSourceReader):
    """跟 `_DictSourceReader`一樣，但 `rows(boom_table)` 在吐出
    `boom_after` 筆之後改拋 `SourceError`——模擬來源資料庫在讀取中途
    才壞掉（source.py 的 `_fetch_iter` 就是這種形狀：`execute()` 當下
    成功，疊代到後面才踩到 `sqlite3.Error`）。
    """

    def __init__(self, *args: Any, boom_table: str, boom_after: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._boom_table = boom_table
        self._boom_after = boom_after

    def rows(self, table: str) -> Iterator[dict[str, Any]]:
        if table != self._boom_table:
            yield from super().rows(table)
            return
        for count, row in enumerate(self._tables.get(table, [])):
            if count == self._boom_after:
                raise SourceError("模擬讀取來源資料庫中途失敗")
            yield row


def _identity(table: str, column: str, model: str, field: str) -> dict[str, Any]:
    return {
        "source": {"table": table, "column": column, "type": "VARCHAR(60)"},
        "target": {"model": model, "field": field, "type": "char"},
        "transform": {
            "kind": "identity",
            "value_map": None,
            "default": None,
            "value": None,
            "ref_table": None,
            "ref_column": None,
        },
    }


def _value_map(
    table: str, column: str, model: str, field: str, mapping: dict[str, Any]
) -> dict[str, Any]:
    return {
        "source": {"table": table, "column": column, "type": "VARCHAR(60)"},
        "target": {"model": model, "field": field, "type": "char"},
        "transform": {
            "kind": "value_map",
            "value_map": mapping,
            "default": None,
            "value": None,
            "ref_table": None,
            "ref_column": None,
        },
    }


def _reference(
    table: str, column: str, model: str, field: str, ref_table: str, ref_column: str
) -> dict[str, Any]:
    return {
        "source": {"table": table, "column": column, "type": "CHAR(16)"},
        "target": {"model": model, "field": field, "type": "many2one"},
        "transform": {
            "kind": "reference",
            "value_map": None,
            "default": None,
            "value": None,
            "ref_table": ref_table,
            "ref_column": ref_column,
        },
    }


def _parent_child_mapping() -> dict[str, Any]:
    """兩張表：`parent`（無相依）、`child`（`parent_ref` 參照 `parent.id`）。"""
    return {
        "models": [
            {"source_table": "parent", "target_model": "res.partner"},
            {"source_table": "child", "target_model": "sale.order"},
        ],
        "mappings": [
            _identity("parent", "name", "res.partner", "name"),
            _reference("child", "parent_ref", "sale.order", "partner_id", "parent", "id"),
            _identity("child", "name", "sale.order", "name"),
        ],
    }


# --------------------------------------------------------------------------
# retry_with_backoff（任務書 Step 2 給的三條，逐字照抄）
# --------------------------------------------------------------------------


def test_retry_with_backoff_retries_then_succeeds():
    """第 1、2 次拋 RateLimited、第 3 次成功 → 總共呼叫 3 次，睡 2 次。

    傳進去的是一個普通函式，不是假的 Odoo——它不模擬 Odoo 的任何業務語意，
    只是把「傳輸層拋了什麼」當參數餵給我們自己的策略函式。
    """
    calls = []
    slept = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RateLimited("429")
        return "ok"

    got = retry_with_backoff(flaky, attempts=5, sleep=slept.append, on_rate_limit=lambda n, d: None)
    assert got == "ok"
    assert len(calls) == 3
    assert len(slept) == 2
    assert slept[1] > slept[0], "退避必須是遞增的，否則不叫指數退避"


def test_retry_with_backoff_gives_up_after_attempts():
    def always():
        raise RateLimited("429")

    with pytest.raises(RateLimited):
        retry_with_backoff(
            always, attempts=3, sleep=lambda d: None, on_rate_limit=lambda n, d: None
        )


def test_retry_does_not_retry_non_rate_limit_errors():
    """OdooError（非 RateLimited）不該被重試——重試一個必然失敗的呼叫
    只會把錯誤延後三倍的時間才浮出來。"""
    calls = []

    def boom():
        calls.append(1)
        raise OdooError("bad request")

    with pytest.raises(OdooError):
        retry_with_backoff(boom, attempts=3, sleep=lambda d: None, on_rate_limit=lambda n, d: None)
    assert len(calls) == 1


# --- 補的測試：任務書三條是下限 ---------------------------------------------


def test_retry_with_backoff_rejects_zero_attempts():
    with pytest.raises(ValueError):
        retry_with_backoff(
            lambda: "x", attempts=0, sleep=lambda d: None, on_rate_limit=lambda n, d: None
        )


def test_retry_with_backoff_succeeds_on_first_call_never_sleeps():
    """第一次就成功時完全不該呼叫 sleep／on_rate_limit——不是「反正最後
    會成功就好」，中途不該有任何不必要的副作用。"""
    slept = []
    rate_limit_calls = []
    got = retry_with_backoff(
        lambda: "ok",
        attempts=5,
        sleep=slept.append,
        on_rate_limit=lambda n, d: rate_limit_calls.append(1),
    )
    assert got == "ok"
    assert slept == []
    assert rate_limit_calls == []


def test_retry_with_backoff_on_rate_limit_called_before_sleep_with_attempt_and_delay():
    """`on_rate_limit(attempt, delay)` 帶的兩個參數必須是「第幾次」與
    「這次要等多久」，而不是巧合對得上的其他數字。"""
    calls = []
    seen: list[tuple[int, float]] = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RateLimited("429")
        return "ok"

    retry_with_backoff(
        flaky, attempts=5, sleep=lambda d: None, on_rate_limit=lambda n, d: seen.append((n, d))
    )
    assert seen == [(1, 1.0)]  # 第一次失敗、延遲是 base delay 本身（2**0）


def test_retry_with_backoff_delays_double_each_time():
    calls = []
    delays: list[float] = []

    def always_rate_limited():
        calls.append(1)
        raise RateLimited("429")

    with pytest.raises(RateLimited):
        retry_with_backoff(
            always_rate_limited,
            attempts=4,
            sleep=lambda d: None,
            on_rate_limit=lambda n, d: delays.append(d),
        )
    assert delays == [1.0, 2.0, 4.0]


# --------------------------------------------------------------------------
# migration_log.jsonl 的寫出與 schema 驗證
# --------------------------------------------------------------------------


def _log_entry(**overrides: Any) -> LogEntry:
    base = {
        "run_id": "r1",
        "source_table": "cust_mast",
        "source_pk": ("C001",),
        "external_id": "migrai.c3.cust_mast.C001",
        "target_model": "res.partner",
        "action": "created",
        "odoo_id": 1,
        "error_code": None,
        "message": "",
    }
    base.update(overrides)
    return LogEntry(**base)


def test_validate_and_dump_returns_json_line_for_valid_entry():
    line = _validate_and_dump(_log_entry())
    payload = json.loads(line)
    assert payload["action"] == "created"
    assert payload["source_pk"] == ["C001"]


def test_validate_and_dump_raises_log_entry_schema_error_when_shape_is_broken():
    """`LogEntry.__post_init__` 已經擋住大部分不合法組合，但沒有檢查
    `source_pk` 每一項是不是字串——這裡故意繞過型別提示（Python 執行期
    不強制）構造一個 schema 會拒絕、但 `__post_init__` 不會拒絕的
    `LogEntry`，確認 `_validate_and_dump` 這第二道防線真的有作用。
    """
    entry = LogEntry(
        run_id="r1",
        source_table="t",
        source_pk=(1, 2),  # type: ignore[arg-type]
        external_id="migrai.c3.t.1-2",
        target_model="m",
        action="created",
        odoo_id=1,
        error_code=None,
        message="",
    )
    with pytest.raises(LogEntrySchemaError):
        _validate_and_dump(entry)


def test_migration_log_writer_creates_empty_file_immediately(tmp_path):
    path = tmp_path / "migration_log.jsonl"
    _MigrationLogWriter(path)
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == ""


def test_migration_log_writer_starts_fresh_each_time(tmp_path):
    path = tmp_path / "migration_log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("殘留的舊內容\n", encoding="utf-8")
    _MigrationLogWriter(path)
    assert path.read_text(encoding="utf-8") == ""


def test_migration_log_writer_append_writes_one_valid_json_line_per_entry(tmp_path):
    path = tmp_path / "migration_log.jsonl"
    writer = _MigrationLogWriter(path)
    writer.append(_log_entry(action="created", odoo_id=1))
    writer.append(_log_entry(action="skipped", odoo_id=None, error_code=None))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "created"
    assert json.loads(lines[1])["action"] == "skipped"


# --------------------------------------------------------------------------
# 純函式：_name_without_module_prefix／_first_error_message／_iter_chunks／
# _merge_decisions
# --------------------------------------------------------------------------


def test_name_without_module_prefix_strips_migrai_dot():
    assert _name_without_module_prefix("migrai.case03.cust_mast.C001") == "case03.cust_mast.C001"


def test_name_without_module_prefix_only_splits_on_first_dot():
    """name 段本身可能含 `.`（encode_pk_value 只保證 external_id 三段各自
    不含未編碼的 `.`，但 module 前綴之後整段仍然是單一字串，這裡確認只切
    第一個點。"""
    assert _name_without_module_prefix("migrai.a.b.c") == "a.b.c"


def test_first_error_message_prefers_type_error_over_other_messages():
    messages = [
        {"type": "warning", "message": "不影響寫入"},
        {"type": "error", "message": "真正的錯誤原因"},
    ]
    assert _first_error_message(messages) == "真正的錯誤原因"


def test_first_error_message_falls_back_to_first_message_when_no_error_typed():
    messages = [{"type": "warning", "message": "只有警告"}]
    assert _first_error_message(messages) == "只有警告"


def test_first_error_message_returns_none_for_empty_or_missing():
    assert _first_error_message([]) is None
    assert _first_error_message(None) is None


def test_first_error_message_does_not_crash_on_unexpected_shape():
    """`messages` 形狀不符預期（不是 list of dict）不該噴裸例外。"""
    assert _first_error_message("not a list") is None
    assert _first_error_message([1, 2, 3]) is None


def test_iter_chunks_splits_into_expected_sizes():
    chunks = list(_iter_chunks(iter([{"i": i} for i in range(5)]), 2))
    assert [len(c) for c in chunks] == [2, 2, 1]
    assert chunks[0][0]["i"] == 0
    assert chunks[-1][0]["i"] == 4


def test_iter_chunks_empty_input_yields_nothing():
    assert list(_iter_chunks(iter([]), 3)) == []


def test_iter_chunks_exact_multiple_does_not_yield_empty_trailing_chunk():
    chunks = list(_iter_chunks(iter([{"i": i} for i in range(4)]), 2))
    assert [len(c) for c in chunks] == [2, 2]


def test_merge_decisions_interleaves_immediate_and_pending_in_original_order():
    """immediate（已經是 LogEntry）與 pending（`_PendingRow`）交錯出現時，
    合併後必須完全照 decisions 原本的順序，不是「先 immediate 後
    pending」或反過來。"""
    immediate_1 = _log_entry(external_id="imm1")
    immediate_2 = _log_entry(external_id="imm2")
    pending_1 = _PendingRow(external_id="p1", name_without_prefix="p1", source_pk=("1",), values=[])
    pending_2 = _PendingRow(external_id="p2", name_without_prefix="p2", source_pk=("2",), values=[])
    result_for_p1 = _log_entry(external_id="p1")
    result_for_p2 = _log_entry(external_id="p2")

    decisions = [immediate_1, pending_1, pending_2, immediate_2]
    merged = _merge_decisions(decisions, [result_for_p1, result_for_p2])

    assert [e.external_id for e in merged] == ["imm1", "p1", "p2", "imm2"]


def test_merge_decisions_all_immediate_ignores_empty_pending_entries():
    immediate_1 = _log_entry(external_id="imm1")
    merged = _merge_decisions([immediate_1], [])
    assert merged == [immediate_1]


# --------------------------------------------------------------------------
# _decide_row：轉換、external_id、懸空外鍵偵測、dry-run（純本地邏輯，不碰
# Odoo）
# --------------------------------------------------------------------------


def _table_plan_for(
    mapping: dict[str, Any], reader: _DictSourceReader, table: str, source_id: str = "t"
):
    plans = plan_load(mapping, reader, source_id=source_id)
    return next(p for p in plans if p.source_table == table)


def test_decide_row_success_returns_pending_row_with_transformed_values():
    mapping = _parent_child_mapping()
    reader = _DictSourceReader(
        tables={
            "parent": [{"id": "P1", "name": "Alice"}],
            "child": [{"id": "C1", "parent_ref": "P1", "name": "order1"}],
        },
        pk={"parent": ["id"], "child": ["id"]},
    )
    parent_plan = _table_plan_for(mapping, reader, "parent")
    log = EventLog(path=Path("/dev/null"), run_id="r1")
    decision = _decide_row(
        {"id": "P1", "name": "Alice"},
        parent_plan,
        ["id"],
        source_id="c3",
        run_id="r1",
        dry_run=False,
        problem_external_ids=set(),
        event_log=log,
    )
    assert isinstance(decision, _PendingRow)
    assert decision.external_id == "migrai.c3.parent.P1"
    assert decision.name_without_prefix == "c3.parent.P1"
    assert decision.source_pk == ("P1",)
    assert decision.values == ["Alice"]


def test_decide_row_dry_run_returns_skipped_entry_naming_dry_run(tmp_path):
    mapping = _parent_child_mapping()
    reader = _DictSourceReader(
        tables={"parent": [{"id": "P1", "name": "Alice"}], "child": []},
        pk={"parent": ["id"], "child": ["id"]},
    )
    parent_plan = _table_plan_for(mapping, reader, "parent")
    log = EventLog(path=tmp_path / "events.jsonl", run_id="r1")
    decision = _decide_row(
        {"id": "P1", "name": "Alice"},
        parent_plan,
        ["id"],
        source_id="c3",
        run_id="r1",
        dry_run=True,
        problem_external_ids=set(),
        event_log=log,
    )
    assert isinstance(decision, LogEntry)
    assert decision.action == "skipped"
    assert decision.error_code is None
    assert "dry-run" in decision.message


def test_decide_row_transform_error_marks_failed_and_emits_e4002(tmp_path):
    mapping = {
        "models": [{"source_table": "parent", "target_model": "res.partner"}],
        "mappings": [_value_map("parent", "name", "res.partner", "name", {"Alice": "A"})],
    }
    reader = _DictSourceReader(
        tables={"parent": [{"id": "P2", "name": "Bob"}]}, pk={"parent": ["id"]}
    )
    plan = _table_plan_for(mapping, reader, "parent")
    log = EventLog(path=tmp_path / "events.jsonl", run_id="r1")
    problem_ids: set[str] = set()

    decision = _decide_row(
        {"id": "P2", "name": "Bob"},
        plan,
        ["id"],
        source_id="c3",
        run_id="r1",
        dry_run=False,
        problem_external_ids=problem_ids,
        event_log=log,
    )
    assert isinstance(decision, LogEntry)
    assert decision.action == "failed"
    assert decision.error_code == "E-4002"
    assert decision.external_id in problem_ids
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["error_code"] == "E-4002" for e in events)


def test_decide_row_null_primary_key_marks_failed_without_crashing(tmp_path):
    """主鍵值為 None（`build_external_id` 會拒絕）不該讓裸 `ValueError` 逃出
    去，必須被轉成 `failed` 的 LogEntry。"""
    mapping = {
        "models": [{"source_table": "parent", "target_model": "res.partner"}],
        "mappings": [_identity("parent", "name", "res.partner", "name")],
    }
    reader = _DictSourceReader(
        tables={"parent": [{"id": None, "name": "Alice"}]}, pk={"parent": ["id"]}
    )
    plan = _table_plan_for(mapping, reader, "parent")
    log = EventLog(path=tmp_path / "events.jsonl", run_id="r1")

    decision = _decide_row(
        {"id": None, "name": "Alice"},
        plan,
        ["id"],
        source_id="c3",
        run_id="r1",
        dry_run=False,
        problem_external_ids=set(),
        event_log=log,
    )
    assert isinstance(decision, LogEntry)
    assert decision.action == "failed"
    assert decision.error_code == "E-4002"
    assert decision.source_pk == ("None",)


def test_decide_row_skips_when_reference_points_to_a_known_problem_id():
    mapping = _parent_child_mapping()
    reader = _DictSourceReader(
        tables={
            "parent": [{"id": "P1", "name": "Alice"}],
            "child": [{"id": "C1", "parent_ref": "P1", "name": "o1"}],
        },
        pk={"parent": ["id"], "child": ["id"]},
    )
    child_plan = _table_plan_for(mapping, reader, "child")
    log = EventLog(path=Path("/dev/null"), run_id="r1")
    problem_ids = {"migrai.c3.parent.P1"}

    decision = _decide_row(
        {"id": "C1", "parent_ref": "P1", "name": "o1"},
        child_plan,
        ["id"],
        source_id="c3",
        run_id="r1",
        dry_run=False,
        problem_external_ids=problem_ids,
        event_log=log,
    )
    assert isinstance(decision, LogEntry)
    assert decision.action == "skipped"
    assert decision.error_code is None
    assert "migrai.c3.parent.P1" in decision.message


def test_decide_row_null_reference_is_not_a_dangling_foreign_key():
    """FK 欄位值為 None（合法的「沒有關聯」）不該被誤判成懸空外鍵。"""
    mapping = _parent_child_mapping()
    reader = _DictSourceReader(
        tables={"parent": [], "child": [{"id": "C1", "parent_ref": None, "name": "o1"}]},
        pk={"parent": ["id"], "child": ["id"]},
    )
    child_plan = _table_plan_for(mapping, reader, "child")
    log = EventLog(path=Path("/dev/null"), run_id="r1")

    decision = _decide_row(
        {"id": "C1", "parent_ref": None, "name": "o1"},
        child_plan,
        ["id"],
        source_id="c3",
        run_id="r1",
        dry_run=False,
        problem_external_ids=set(),
        event_log=log,
    )
    assert isinstance(decision, _PendingRow)


# --------------------------------------------------------------------------
# run_load：dry-run 端到端（走完讀取/轉換/排序/組值，不連 Odoo）
# --------------------------------------------------------------------------


def test_run_load_dry_run_requires_no_client():
    """dry_run=True 時 client 允許是 None（--dry-run 是為本機沒有 Odoo 的
    貢獻者服務的），這是唯一容許 client=None 的情況。"""
    mapping = _parent_child_mapping()
    reader = _DictSourceReader(
        tables={"parent": [{"id": "P1", "name": "Alice"}], "child": []},
        pk={"parent": ["id"], "child": ["id"]},
    )
    # 不拋例外即為通過。
    run_load(
        mapping,
        reader,
        None,
        source_id="c3",
        out_dir=Path("/tmp"),  # noqa: S108 - 不會真的寫出檔案外的東西，下方用 tmp_path 版本才驗證內容
        event_log=EventLog(path=Path("/dev/null"), run_id="r1"),
        dry_run=True,
    )


def test_run_load_non_dry_run_without_client_raises_value_error(tmp_path):
    mapping = _parent_child_mapping()
    reader = _DictSourceReader(
        tables={"parent": [], "child": []}, pk={"parent": ["id"], "child": ["id"]}
    )
    with pytest.raises(ValueError):
        run_load(
            mapping,
            reader,
            None,
            source_id="c3",
            out_dir=tmp_path,
            event_log=EventLog(path=tmp_path / "events.jsonl", run_id="r1"),
            dry_run=False,
        )


def test_run_load_rejects_batch_size_below_one(tmp_path):
    mapping = _parent_child_mapping()
    reader = _DictSourceReader(
        tables={"parent": [], "child": []}, pk={"parent": ["id"], "child": ["id"]}
    )
    with pytest.raises(ValueError):
        run_load(
            mapping,
            reader,
            None,
            source_id="c3",
            out_dir=tmp_path,
            event_log=EventLog(path=tmp_path / "events.jsonl", run_id="r1"),
            batch_size=0,
            dry_run=True,
        )


def test_run_load_dry_run_writes_migration_log_with_correct_topological_order(tmp_path):
    mapping = _parent_child_mapping()
    reader = _DictSourceReader(
        tables={
            "parent": [{"id": "P1", "name": "Alice"}, {"id": "P2", "name": "Bob"}],
            "child": [
                {"id": "C1", "parent_ref": "P1", "name": "o1"},
                {"id": "C2", "parent_ref": "P2", "name": "o2"},
            ],
        },
        pk={"parent": ["id"], "child": ["id"]},
    )
    out = tmp_path / "out"
    result = run_load(
        mapping,
        reader,
        None,
        source_id="c3",
        out_dir=out,
        event_log=EventLog(path=out / "events.jsonl", run_id="r1"),
        dry_run=True,
    )

    assert (out / "migration_log.jsonl").is_file()
    entries = [
        json.loads(line)
        for line in (out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 4 == len(result.entries)
    assert {e["action"] for e in entries} == {"skipped"}
    tables = [e["source_table"] for e in entries]
    assert tables.index("parent") < tables.index("child")
    child_entry = next(
        e for e in entries if e["source_table"] == "child" and "C1" in e["source_pk"]
    )
    assert child_entry["external_id"] == "migrai.c3.child.C1"


def test_run_load_dry_run_distinguishes_parent_failure_skip_from_plain_dry_run_skip(tmp_path):
    """同一場 dry-run 裡：一筆 parent 因 TransformError 失敗
    （E-4002），引用它的 child 要標「上游失敗」而非「dry-run」；引用另一筆
    健康 parent 的 child 則正常標「dry-run」。兩者的 action 都是
    `skipped`，只有 message 不同——這條測試就是要分辨這個差異，不能只
    看 action。"""
    mapping = {
        "models": [
            {"source_table": "parent", "target_model": "res.partner"},
            {"source_table": "child", "target_model": "sale.order"},
        ],
        "mappings": [
            _value_map("parent", "name", "res.partner", "name", {"Alice": "A"}),
            _reference("child", "parent_ref", "sale.order", "partner_id", "parent", "id"),
        ],
    }
    reader = _DictSourceReader(
        tables={
            "parent": [{"id": "P1", "name": "Alice"}, {"id": "P2", "name": "Bob"}],
            "child": [
                {"id": "C1", "parent_ref": "P1"},
                {"id": "C2", "parent_ref": "P2"},
            ],
        },
        pk={"parent": ["id"], "child": ["id"]},
    )
    out = tmp_path / "out"
    result = run_load(
        mapping,
        reader,
        None,
        source_id="c3",
        out_dir=out,
        event_log=EventLog(path=out / "events.jsonl", run_id="r1"),
        dry_run=True,
    )

    by_id = {e.source_pk: e for e in result.entries}
    p1 = by_id[("P1",)]
    p2 = by_id[("P2",)]
    c1 = by_id[("C1",)]
    c2 = by_id[("C2",)]

    assert p1.action == "skipped" and "dry-run" in p1.message
    assert p2.action == "failed" and p2.error_code == "E-4002"
    assert c1.action == "skipped" and "dry-run" in c1.message
    assert c2.action == "skipped" and "上游記錄" in c2.message and p2.external_id in c2.message


def test_run_load_cyclic_reference_aborts_with_e4001_and_empty_log(tmp_path):
    mapping = {
        "models": [
            {"source_table": "a_tbl", "target_model": "model.a"},
            {"source_table": "b_tbl", "target_model": "model.b"},
        ],
        "mappings": [
            _reference("a_tbl", "b_no", "model.a", "b_id", "b_tbl", "id"),
            _reference("b_tbl", "a_no", "model.b", "a_id", "a_tbl", "id"),
        ],
    }
    reader = _DictSourceReader(
        tables={"a_tbl": [{"id": "A1", "b_no": "B1"}], "b_tbl": [{"id": "B1", "a_no": "A1"}]},
        pk={"a_tbl": ["id"], "b_tbl": ["id"]},
    )
    out = tmp_path / "out"
    log = EventLog(path=out / "events.jsonl", run_id="r1")
    result = run_load(
        mapping, reader, None, source_id="c3", out_dir=out, event_log=log, dry_run=True
    )

    assert result.entries == ()
    assert (out / "migration_log.jsonl").is_file()
    assert (out / "migration_log.jsonl").read_text(encoding="utf-8") == ""
    assert any(e["error_code"] == "E-4001" for e in log.events)

    # 反向對照：確認這確實真的是循環參照，而不是這份 fixture 本身有別的
    # 結構性問題（plan_load 對同一份 mapping 應該直接拋 CyclicReference）。
    with pytest.raises(CyclicReference):
        plan_load(mapping, reader, source_id="c3")


def test_run_load_source_error_mid_iteration_aborts_but_keeps_already_written_entries(tmp_path):
    """來源資料庫在讀取中途（不是一開始）才壞掉：已經處理完並寫出的列
    不能因為中止而從 `migration_log.jsonl` 消失。"""
    mapping = {
        "models": [{"source_table": "parent", "target_model": "res.partner"}],
        "mappings": [_identity("parent", "name", "res.partner", "name")],
    }
    reader = _FlakyReader(
        tables={
            "parent": [
                {"id": "P1", "name": "Alice"},
                {"id": "P2", "name": "Bob"},
                {"id": "P3", "name": "Carol"},
            ]
        },
        pk={"parent": ["id"]},
        boom_table="parent",
        boom_after=1,
    )
    out = tmp_path / "out"
    log = EventLog(path=out / "events.jsonl", run_id="r1")
    # batch_size=1：每筆各自成一個 chunk，boom 發生在嘗試讀第 2 筆時，
    # 此時第 1 筆已經處理完並寫出——不受這次中止影響。
    result = run_load(
        mapping,
        reader,
        None,
        source_id="c3",
        out_dir=out,
        event_log=log,
        dry_run=True,
        batch_size=1,
    )

    assert len(result.entries) == 1
    assert result.entries[0].source_pk == ("P1",)
    lines = (out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert any(e["error_code"] == "E-4001" for e in log.events)
