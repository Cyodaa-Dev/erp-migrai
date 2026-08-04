"""`migrai verify` 的 CLI 契約：exit code、密碼來源、「報告永遠產出」。

跟 `test_cli_load.py`（load 的 CLI 契約）刻意保持相同的結構與訊息風格。

⚠️ 誠實界線：`verify` 沒有 `--dry-run`（設計 spec §3.1：驗證階段本來就是
要去讀 Odoo 裡真的寫了什麼，沒有「不連線」這個選項），所以這裡只測得到
**連線之前**就會失敗的路徑（缺檔案、缺密碼、缺必要選項……），以及
**連線本身注定失敗**（loopback、沒有服務監聽）的路徑——後者用來確認密碼
沒有外洩、`OdooError` 被收斂成清楚的中文訊息，而不是驗證「連上真 Odoo
後比對邏輯正不正確」，那一半全部留給 Task 12 的 CI 整合測試。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

import pytest
from typer.testing import CliRunner

from erp_migrai.cli import _resolve_source_counts, app, write_reports
from erp_migrai.pipeline.report import build_report
from erp_migrai.pipeline.source import SqliteSourceReader
from erp_migrai.pipeline.verify import Check

_IS_ROOT = hasattr(os, "getuid") and os.getuid() == 0

runner = CliRunner()

# --------------------------------------------------------------------------
# 一份最小、自我完備的 mapping.json + 來源資料庫 + migration_log.jsonl。
# --------------------------------------------------------------------------

_MINIMAL_MAPPING = {
    "schema_version": "1.1",
    "inventory_synthetic": True,
    "stats": {
        "source_columns": 1,
        "mapped_columns": 1,
        "coverage_percent": 100,
        "auto": 1,
        "suggested": 0,
        "needs_human": 0,
    },
    "models": [
        {
            "source_table": "cust",
            "target_model": "res.partner",
            "confidence": 100,
            "tier": "auto",
            "reasons": ["r"],
        }
    ],
    "mappings": [
        {
            "source": {"table": "cust", "column": "name", "type": "CHAR(10)"},
            "target": {"model": "res.partner", "field": "name", "type": "char"},
            "transform": {
                "kind": "identity",
                "value_map": None,
                "default": None,
                "value": None,
                "ref_table": None,
                "ref_column": None,
            },
            "confidence": 100,
            "tier": "auto",
            "reasons": ["r"],
        }
    ],
    "unmapped": [],
    "conflicts": [],
}

_LOG_ENTRY = {
    "run_id": "r1",
    "source_table": "cust",
    "source_pk": ["C001"],
    "external_id": "migrai.t1.cust.C001",
    "target_model": "res.partner",
    "action": "created",
    "odoo_id": 1,
    "error_code": None,
    "message": "",
}


@pytest.fixture
def minimal_source(tmp_path):
    db_path = tmp_path / "data.sqlite"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE cust (id CHAR(4) NOT NULL, name CHAR(20), PRIMARY KEY (id))")
        con.execute("INSERT INTO cust VALUES ('C001', 'Alice')")
        con.commit()
    return db_path


@pytest.fixture
def minimal_mapping_path(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(_MINIMAL_MAPPING), encoding="utf-8")
    return path


@pytest.fixture
def minimal_log_path(tmp_path):
    path = tmp_path / "migration_log.jsonl"
    path.write_text(json.dumps(_LOG_ENTRY) + "\n", encoding="utf-8")
    return path


def _connect_args(mapping_path, source_path, log_path, out) -> list[str]:
    """連線資訊指到 loopback 的 1 號埠——沒有任何服務監聽，連線立刻被拒絕。
    不是在測「連上真 Odoo 後的行為」，是測「連線注定失敗的路徑上，密碼
    有沒有外洩、錯誤有沒有被收斂成清楚的中文訊息」（precedent：
    test_cli_load.py 的 test_password_env_var_never_leaks_and_is_actually_read）。
    """
    return [
        "verify",
        "--mapping",
        str(mapping_path),
        "--source",
        str(source_path),
        "--log",
        str(log_path),
        "--source-id",
        "t1",
        "--odoo-url",
        "http://127.0.0.1:1",
        "--odoo-db",
        "migrai",
        "--odoo-user",
        "admin",
        "--out",
        str(out),
    ]


# --------------------------------------------------------------------------
# 用法錯誤 → exit 2
# --------------------------------------------------------------------------


def test_missing_required_option_exits_two():
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2


def test_no_dry_run_flag_exists():
    """驗證階段沒有 --dry-run（見模組 docstring：沒有「不連線」這個選項）。"""
    result = runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" not in result.output


def test_no_password_flag_exists():
    result = runner.invoke(app, ["verify", "--help"])
    assert "--password-stdin" in result.output
    assert "--password " not in result.output.replace("--password-stdin", "")


# --------------------------------------------------------------------------
# --log 指到不存在的檔案 → exit 1，訊息點名路徑，無 Traceback（任務書
# Step 4 明文要求的那一條）
# --------------------------------------------------------------------------


def test_missing_log_file_exits_one_without_traceback(
    minimal_mapping_path, minimal_source, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    missing_log = tmp_path / "nope.jsonl"
    out = tmp_path / "out"
    result = runner.invoke(
        app, _connect_args(minimal_mapping_path, minimal_source, missing_log, out)
    )
    assert result.exit_code == 1
    assert str(missing_log) in result.stdout
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


# --------------------------------------------------------------------------
# --log 內容不合法：不是 JSONL／每行 schema 不合——不能讓裸例外逃出去
# --------------------------------------------------------------------------


def test_log_with_invalid_json_exits_one_without_traceback(
    minimal_mapping_path, minimal_source, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    bad_log = tmp_path / "bad.jsonl"
    bad_log.write_text("{ 這不是合法的 JSON", encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, _connect_args(minimal_mapping_path, minimal_source, bad_log, out))
    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert "migration_log.jsonl" in result.stdout


def test_log_violating_schema_exits_one_without_traceback(
    minimal_mapping_path, minimal_source, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    bad_entry = {**_LOG_ENTRY, "action": "archived"}  # 不在 ACTIONS 的四個值裡
    bad_log = tmp_path / "bad.jsonl"
    bad_log.write_text(json.dumps(bad_entry) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, _connect_args(minimal_mapping_path, minimal_source, bad_log, out))
    assert result.exit_code == 1
    assert "Traceback" not in result.stdout


def test_empty_log_file_is_not_a_format_error(
    minimal_mapping_path, minimal_source, tmp_path, monkeypatch
):
    """空的 `migration_log.jsonl` 是合法狀態（例如載入階段一開始就中止而
    一筆都沒寫），不該被 --log 的格式檢查擋下——這個測試只確認它不會在
    『解析 --log』這一步就以格式錯誤結束；後面連線 Odoo 失敗是另一回事，
    exit code 一樣是 1，但訊息點名的必須是連線失敗，不是 --log 格式錯誤。
    """
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    empty_log = tmp_path / "empty.jsonl"
    empty_log.write_text("", encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, _connect_args(minimal_mapping_path, minimal_source, empty_log, out))
    assert result.exit_code == 1
    assert "migration_log.jsonl 不是合法的" not in result.stdout
    assert "連線 Odoo 失敗" in result.stdout


# --------------------------------------------------------------------------
# --mapping／--source 指到不存在或壞掉的檔案
# --------------------------------------------------------------------------


def test_missing_mapping_file_exits_one_without_traceback(
    minimal_source, minimal_log_path, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    missing = tmp_path / "nope.json"
    out = tmp_path / "out"
    result = runner.invoke(app, _connect_args(missing, minimal_source, minimal_log_path, out))
    assert result.exit_code == 1
    assert str(missing) in result.stdout
    assert "Traceback" not in result.stdout


def test_mapping_invalid_json_exits_one_without_traceback(
    minimal_source, minimal_log_path, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    bad = tmp_path / "bad.json"
    bad.write_text("{ 這不是合法的 JSON", encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, _connect_args(bad, minimal_source, minimal_log_path, out))
    assert result.exit_code == 1
    assert "Traceback" not in result.stdout


def test_missing_source_database_exits_one_without_traceback(
    minimal_mapping_path, minimal_log_path, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    missing = tmp_path / "nope.sqlite"
    out = tmp_path / "out"
    result = runner.invoke(app, _connect_args(minimal_mapping_path, missing, minimal_log_path, out))
    assert result.exit_code == 1
    assert str(missing) in result.stdout
    assert "Traceback" not in result.stdout


# --------------------------------------------------------------------------
# 密碼來源：--password-stdin 或 MIGRAI_ODOO_PASSWORD，缺兩者都給清楚指示
# --------------------------------------------------------------------------


def test_missing_password_exits_nonzero_with_clear_message(
    minimal_mapping_path, minimal_source, minimal_log_path, tmp_path, monkeypatch
):
    monkeypatch.delenv("MIGRAI_ODOO_PASSWORD", raising=False)
    out = tmp_path / "out"
    result = runner.invoke(
        app, _connect_args(minimal_mapping_path, minimal_source, minimal_log_path, out)
    )
    assert result.exit_code in (1, 2)
    assert "密碼" in result.stdout
    assert "Traceback" not in result.stdout


# --------------------------------------------------------------------------
# --out 被普通檔案占用 → exit 1，無 traceback
# --------------------------------------------------------------------------


def test_unwritable_out_dir_exits_one_without_traceback(
    minimal_mapping_path, minimal_source, minimal_log_path, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    blocked = tmp_path / "blocked"
    blocked.write_text("我是一個檔案，不是目錄", encoding="utf-8")

    result = runner.invoke(
        app, _connect_args(minimal_mapping_path, minimal_source, minimal_log_path, blocked)
    )

    assert result.exit_code == 1
    assert str(blocked) in result.stdout
    assert "Traceback" not in result.stdout


# --------------------------------------------------------------------------
# 連線注定失敗（loopback :1）：密碼不外洩，訊息清楚，不寫出假報告
# --------------------------------------------------------------------------


def test_connection_failure_does_not_leak_password_and_writes_no_report(
    minimal_mapping_path, minimal_source, minimal_log_path, tmp_path, monkeypatch
):
    """連 Odoo 都連不上時，這裡選擇**不**寫出報告——一份全是缺失或假數字
    的報告比不寫更容易誤導使用者（見 `cli.verify_command` docstring）。
    這跟『檢查真的跑完但結果是 fail』要永遠寫報告是兩回事，那個情境見
    `tests/pipeline/test_verify.py`／`test_report.py` 對純函式的覆蓋
    ——CLI 這一層連不上 Odoo，本來就拿不到任何真數字可以誠實地寫進報告。
    """
    secret = "sup3rs3cret"  # noqa: S105 - 測試用假密碼，不是真的密鑰
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", secret)
    out = tmp_path / "out"

    result = runner.invoke(
        app, _connect_args(minimal_mapping_path, minimal_source, minimal_log_path, out)
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert secret not in result.stdout
    assert "連線 Odoo 失敗" in result.stdout
    assert not (out / "verification_report.json").exists()
    assert not (out / "verification_report.md").exists()

    events_path = out / "events.jsonl"
    if events_path.is_file():
        assert secret not in events_path.read_text(encoding="utf-8")


def test_connection_failure_does_not_require_odoo_reachable_to_validate_log_and_mapping_first(
    minimal_mapping_path, minimal_source, minimal_log_path, tmp_path, monkeypatch
):
    """檔案層級的檢查（--log／--mapping／--source／密碼）全部先跑完，才會
    真的嘗試連線——這條測試用『訊息點名的是連線失敗，不是檔案缺失』來
    確認前面幾層真的先通過了，而不是巧合地都在同一個 exit 1 裡。"""
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    out = tmp_path / "out"
    result = runner.invoke(
        app, _connect_args(minimal_mapping_path, minimal_source, minimal_log_path, out)
    )
    assert result.exit_code == 1
    assert "連線 Odoo 失敗" in result.stdout
    assert "--log" not in result.stdout
    assert "--mapping" not in result.stdout


def test_odoo_connection_failure_message_has_no_duplicated_prefix(
    minimal_mapping_path, minimal_source, minimal_log_path, tmp_path, monkeypatch
):
    """審查修正輪次 1（順手修）：`OdooError` 的訊息本身在連線被拒絕這種
    情境下已經含「連線 Odoo 失敗：」（`odoo_client.py` 的 `_send()`），
    `cli.py` 不該再疊加一次同樣的前綴，變成「連線 Odoo 失敗：連線 Odoo
    失敗：……」。"""
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    out = tmp_path / "out"
    result = runner.invoke(
        app, _connect_args(minimal_mapping_path, minimal_source, minimal_log_path, out)
    )
    assert result.exit_code == 1
    assert "連線 Odoo 失敗：連線 Odoo 失敗" not in result.stdout


# --------------------------------------------------------------------------
# --log 的 external_id 必須對應這次 --source-id（審查修正輪次 1 Critical
# C4／控制者裁決）——純字串比對，連線 Odoo 之前就能擋下，不需要 Odoo。
# --------------------------------------------------------------------------


def test_log_with_mismatched_source_id_exits_one_before_connecting_to_odoo(
    minimal_mapping_path, minimal_source, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    mismatched_entry = {**_LOG_ENTRY, "external_id": "migrai.other_source.cust.C001"}
    bad_log = tmp_path / "mismatched.jsonl"
    bad_log.write_text(json.dumps(mismatched_entry) + "\n", encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, _connect_args(minimal_mapping_path, minimal_source, bad_log, out))

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert "不符" in result.stdout
    assert "migrai.other_source.cust.C001" in result.stdout
    # 必須在連線之前就擋下——不該看到連線失敗的訊息。
    assert "連線 Odoo 失敗" not in result.stdout
    assert not (out / "verification_report.json").exists()


def test_log_with_matching_source_id_passes_the_check(
    minimal_mapping_path, minimal_source, minimal_log_path, tmp_path, monkeypatch
):
    """反例：`minimal_log_path`／`_connect_args` 用的 `--source-id` 都是
    `t1`，`_LOG_ENTRY` 的 external_id 也是 `migrai.t1.cust.C001`——這個
    檢查必須放行，執行照樣往下走到連線 Odoo 那一步（loopback :1 必然
    連線失敗，但那是後面的事，不是這裡擋下來的）。
    """
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    out = tmp_path / "out"
    result = runner.invoke(
        app, _connect_args(minimal_mapping_path, minimal_source, minimal_log_path, out)
    )
    assert result.exit_code == 1
    assert "不符" not in result.stdout
    assert "連線 Odoo 失敗" in result.stdout


# --------------------------------------------------------------------------
# _resolve_source_counts：source_count 的分母只能來自 reader.count()
# （審查修正輪次 1 Critical C1／M61）
# --------------------------------------------------------------------------


def test_resolve_source_counts_uses_reader_count_not_migration_log(tmp_path):
    """`source_count` 的定義是「來源表的總筆數」，唯一合法來源是
    `SourceReader.count()`——不可以用『migration_log 裡非 failed 的筆數』
    代替。這裡建一張有 3 筆的來源表，並且用註解文件化一份『假想的』
    migration_log 只回報 2 筆成功：`_resolve_source_counts` 的簽章根本
    沒有接受 log 的參數，想犯這個錯誤得先幫函式簽章加一個參數。
    """
    db_path = tmp_path / "data.sqlite"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE cust (id CHAR(4) NOT NULL, name CHAR(20), PRIMARY KEY (id))")
        con.executemany(
            "INSERT INTO cust VALUES (?, ?)",
            [("C001", "Alice"), ("C002", "Bob"), ("C003", "Carol")],
        )
        con.commit()

    # 假想中一份 migration_log：只有 2 筆 created、1 筆 failed——如果分母
    # 錯誤地取「非 failed 的筆數」，會算出 2，而不是來源真正的 3。這個
    # 變數只是把陷阱寫進測試文件，不會被餵給 `_resolve_source_counts`。
    hypothetical_log_derived_count = 2
    assert hypothetical_log_derived_count != 3, "這條測試本身要能分辨兩個數字"

    with SqliteSourceReader(db_path) as reader:
        counts = _resolve_source_counts(reader, [("cust", "res.partner")])

    assert counts == {"cust": 3}


def test_resolve_source_counts_covers_every_target_table(tmp_path):
    db_path = tmp_path / "data.sqlite"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE a (id CHAR(4) NOT NULL, PRIMARY KEY (id))")
        con.execute("CREATE TABLE b (id CHAR(4) NOT NULL, PRIMARY KEY (id))")
        con.execute("INSERT INTO a VALUES ('A1')")
        con.executemany("INSERT INTO b VALUES (?)", [("B1",), ("B2",)])
        con.commit()

    with SqliteSourceReader(db_path) as reader:
        counts = _resolve_source_counts(reader, [("a", "res.partner"), ("b", "sale.order")])

    assert counts == {"a": 1, "b": 2}


# --------------------------------------------------------------------------
# write_reports：報告永遠先寫出，才判斷 exit code（審查修正輪次 1
# Critical C2 的部分覆蓋——真正端到端留給 Task 12）
# --------------------------------------------------------------------------


def test_write_reports_writes_both_files_even_when_all_checks_fail(tmp_path):
    checks = [Check("row_count", "cust_mast", "res.partner", "fail", 3, 1, None, None)]
    report = build_report(checks, source_id="case03", synthetic=True)
    json_path = tmp_path / "verification_report.json"
    md_path = tmp_path / "verification_report.md"

    write_reports(report, json_path=json_path, md_path=md_path)

    assert json_path.exists()
    assert md_path.exists()
    written = json.loads(json_path.read_text(encoding="utf-8"))
    assert written["summary"]["failed"] == 1
    assert "❌" in md_path.read_text(encoding="utf-8")
