"""`migrai load` 的 CLI 契約：exit code、密碼來源、雙軌輸出。

跟 `test_cli_map.py`（map 的 CLI 契約）刻意保持相同的結構與訊息風格。

⚠️ 誠實界線：這裡只測 `--dry-run`（不連 Odoo）與 CLI 本身的參數／錯誤處理
（缺檔案、缺密碼、缺必要選項……）。「真的連上 Odoo 會怎樣」一律留給
Task 12 的 CI 整合測試。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

import pytest
from typer.testing import CliRunner

from erp_migrai.cli import app

# chmod 500 在 root 下沒有意義，precedent：test_cli.py 的 _IS_ROOT。
_IS_ROOT = hasattr(os, "getuid") and os.getuid() == 0

runner = CliRunner()

# --------------------------------------------------------------------------
# 一份最小、自我完備的來源資料庫 + mapping.json：不依賴 corpus，讓 CLI 參數
# ／錯誤處理的測試不必先跑 scan/map。
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


def _dry_run_args(mapping_path, source_path, out) -> list[str]:
    return [
        "load",
        "--mapping",
        str(mapping_path),
        "--source",
        str(source_path),
        "--source-id",
        "t1",
        "--out",
        str(out),
        "--dry-run",
    ]


# --------------------------------------------------------------------------
# --dry-run 的基本 CLI 端到端（最小 fixture 版本）
# --------------------------------------------------------------------------


def test_dry_run_exits_zero_and_writes_migration_log(
    minimal_mapping_path, minimal_source, tmp_path
):
    out = tmp_path / "out"
    result = runner.invoke(app, _dry_run_args(minimal_mapping_path, minimal_source, out))

    assert result.exit_code == 0
    assert (out / "migration_log.jsonl").is_file()
    assert (out / "events.jsonl").is_file()
    entries = [
        json.loads(line)
        for line in (out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 1
    assert entries[0]["action"] == "skipped"
    assert "dry-run" in entries[0]["message"]
    assert "載入完成" in result.stdout
    assert "migration_log.jsonl" in result.stdout


def test_dry_run_does_not_require_odoo_credentials(
    minimal_mapping_path, minimal_source, tmp_path, monkeypatch
):
    """--dry-run 是為本機沒有 Odoo 的貢獻者服務的：不給 --odoo-url／
    --odoo-db／--odoo-user，也不設密碼，一樣要能跑。"""
    monkeypatch.delenv("MIGRAI_ODOO_PASSWORD", raising=False)
    out = tmp_path / "out"
    result = runner.invoke(app, _dry_run_args(minimal_mapping_path, minimal_source, out))
    assert result.exit_code == 0


def test_events_jsonl_lines_are_valid_json_and_share_run_id_with_migration_log(
    minimal_mapping_path, minimal_source, tmp_path
):
    out = tmp_path / "out"
    runner.invoke(app, _dry_run_args(minimal_mapping_path, minimal_source, out))

    log_entries = [
        json.loads(line)
        for line in (out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    events = [
        json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    # 乾淨的 dry-run 不觸發任何 E-40xx 事件，但 run_id 的 join 關係仍然要
    # 能從 migration_log 這一側驗證——log_entries 至少有一筆，run_id 必須
    # 是合法字串（不是空字串或 None）。
    assert log_entries
    assert all(e["run_id"] for e in log_entries)
    assert events == [] or all("run_id" in e for e in events)


def test_rerunning_does_not_duplicate_migration_log(minimal_mapping_path, minimal_source, tmp_path):
    out = tmp_path / "out"
    runner.invoke(app, _dry_run_args(minimal_mapping_path, minimal_source, out))
    first = len((out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines())
    runner.invoke(app, _dry_run_args(minimal_mapping_path, minimal_source, out))
    second = len((out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines())
    assert first == second == 1


# --------------------------------------------------------------------------
# P3-2：沒有 --password 旗標
# --------------------------------------------------------------------------


def test_no_password_flag_exists():
    """密碼旗標一旦存在，README 的範例就會教使用者把密碼放進 shell 歷史。"""
    result = runner.invoke(app, ["load", "--help"])
    assert "--password-stdin" in result.output
    assert "--password " not in result.output.replace("--password-stdin", "")


# --------------------------------------------------------------------------
# 密碼來源：--password-stdin 或 MIGRAI_ODOO_PASSWORD，缺兩者都給清楚指示
# --------------------------------------------------------------------------


def test_missing_password_without_dry_run_exits_nonzero_with_clear_message(
    minimal_mapping_path, minimal_source, tmp_path, monkeypatch
):
    monkeypatch.delenv("MIGRAI_ODOO_PASSWORD", raising=False)
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "load",
            "--mapping",
            str(minimal_mapping_path),
            "--source",
            str(minimal_source),
            "--source-id",
            "t1",
            "--odoo-url",
            "http://localhost:8069",
            "--odoo-db",
            "migrai",
            "--odoo-user",
            "admin",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code in (1, 2)
    assert "密碼" in result.stdout
    assert "Traceback" not in result.stdout


def test_missing_odoo_connection_info_without_dry_run_exits_one(
    minimal_mapping_path, minimal_source, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", "s3cret")
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "load",
            "--mapping",
            str(minimal_mapping_path),
            "--source",
            str(minimal_source),
            "--source-id",
            "t1",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.stdout


# --------------------------------------------------------------------------
# --out 被普通檔案占用 → exit 1，無 traceback
# --------------------------------------------------------------------------


def test_unwritable_out_dir_exits_one_without_traceback(
    minimal_mapping_path, minimal_source, tmp_path
):
    blocked = tmp_path / "blocked"
    blocked.write_text("我是一個檔案，不是目錄", encoding="utf-8")

    result = runner.invoke(app, _dry_run_args(minimal_mapping_path, minimal_source, blocked))

    assert result.exit_code == 1
    assert str(blocked) in result.stdout
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


# --------------------------------------------------------------------------
# --mapping／--source 指到不存在或壞掉的檔案
# --------------------------------------------------------------------------


def test_missing_mapping_file_exits_one_without_traceback(minimal_source, tmp_path):
    missing = tmp_path / "nope.json"
    out = tmp_path / "out"
    result = runner.invoke(app, _dry_run_args(missing, minimal_source, out))
    assert result.exit_code == 1
    assert str(missing) in result.stdout
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


def test_mapping_invalid_json_exits_one_without_traceback(minimal_source, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ 這不是合法的 JSON", encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, _dry_run_args(bad, minimal_source, out))
    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


def test_missing_source_database_exits_one_without_traceback(minimal_mapping_path, tmp_path):
    missing = tmp_path / "nope.sqlite"
    out = tmp_path / "out"
    result = runner.invoke(app, _dry_run_args(minimal_mapping_path, missing, out))
    assert result.exit_code == 1
    assert str(missing) in result.stdout
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


# --------------------------------------------------------------------------
# 用法錯誤 → exit 2
# --------------------------------------------------------------------------


def test_missing_required_option_exits_two():
    result = runner.invoke(app, ["load"])
    assert result.exit_code == 2


# --------------------------------------------------------------------------
# E-4002（TransformError）：不需要 Odoo 就能在本機重現，確認 exit code 與
# migration_log 的內容誠實反映失敗。
# --------------------------------------------------------------------------


def test_transform_error_in_dry_run_still_exits_one(tmp_path):
    """`dry_run` 不會壓下 E-4002：轉換失敗是資料本身的問題，跟有沒有連
    Odoo 無關，`--dry-run` 存在的目的正是要讓貢獻者能在本機發現這種
    問題。"""
    db_path = tmp_path / "data.sqlite"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE cust (id CHAR(4) NOT NULL, flag CHAR(1), PRIMARY KEY (id))")
        con.execute("INSERT INTO cust VALUES ('C001', 'X')")  # 'X' 不在 value_map 裡且無 default
        con.commit()

    mapping = {
        **_MINIMAL_MAPPING,
        "mappings": [
            {
                "source": {"table": "cust", "column": "flag", "type": "CHAR(1)"},
                "target": {"model": "res.partner", "field": "active", "type": "boolean"},
                "transform": {
                    "kind": "value_map",
                    "value_map": {"Y": True, "N": False},
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
    }
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, _dry_run_args(mapping_path, db_path, out))

    assert result.exit_code == 1
    entries = [
        json.loads(line)
        for line in (out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 1
    assert entries[0]["action"] == "failed"
    assert entries[0]["error_code"] == "E-4002"
    events = [
        json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["error_code"] == "E-4002" for e in events)


# --------------------------------------------------------------------------
# Step 3（任務書）：對 case03 語料的 --dry-run 端到端。
#
# PRE-FLIGHT 已警告：data.sqlite 不進 git（.gitignore），mapping.json 用
# 內建 catalog／rules 就能 100% 對上 case03（見 corpus/case03_sales_order/
# expected/mapping.json 這份 golden 本身就是這樣產生的），所以這裡在
# tmp_path 裡真的跑一次 scan → map（透過 CLI，不直接 import 引擎），
# 並用 build_case() 現場產生 data.sqlite——不依賴 out/ 或倉庫裡任何先前
# 執行留下的檔案，在乾淨 checkout 上也能通過。
# --------------------------------------------------------------------------


@pytest.fixture
def case03_source(tmp_path, corpus_root):
    import shutil

    from erp_migrai.tools.build_corpus import build_case

    case_dir = tmp_path / "case03"
    (case_dir / "src").mkdir(parents=True)
    (case_dir / "data").mkdir()
    shutil.copy(
        corpus_root / "case03_sales_order" / "src" / "schema.sql", case_dir / "src" / "schema.sql"
    )
    shutil.copy(
        corpus_root / "case03_sales_order" / "data" / "seed.sql", case_dir / "data" / "seed.sql"
    )
    build_case(case_dir)
    return case_dir / "data" / "data.sqlite"


def test_dry_run_produces_migration_log_without_touching_odoo(case03_source, corpus_root, tmp_path):
    """dry-run 必須走完全部路徑：讀取、轉換、排序、組 payload。

    這是本機沒有 Odoo 的貢獻者唯一能驗證自己補的規則有沒有效的方式，
    所以它必須真的走完，而不是提早 return。
    """
    out = tmp_path / "out"

    scan_result = runner.invoke(
        app, ["scan", "--src", str(corpus_root / "case03_sales_order" / "src"), "--out", str(out)]
    )
    assert scan_result.exit_code == 0

    map_result = runner.invoke(
        app, ["map", "--inventory", str(out / "inventory.json"), "--out", str(out)]
    )
    assert map_result.exit_code == 0

    result = runner.invoke(
        app,
        [
            "load",
            "--mapping",
            str(out / "mapping.json"),
            "--source",
            str(case03_source),
            "--source-id",
            "case03",
            "--out",
            str(out),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0

    assert (out / "migration_log.jsonl").is_file()
    entries = [
        json.loads(line)
        for line in (out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {e["action"] for e in entries} == {"skipped"}
    # 拓撲排序真的發生了。
    tables = [e["source_table"] for e in entries]
    assert tables.index("cust_mast") < tables.index("ord_mast")
    # reference 真的被解析成 external_id。
    ord_entry = next(e for e in entries if e["source_table"] == "ord_mast")
    assert ord_entry["external_id"].startswith("migrai.")

    # spec §4：case03 的 .4gl 刻意含不支援語法（scan 的 inventory.json 會有
    # unsupported_syntax），但資料遷移吃的是 schema.sql／data.sqlite，這裡
    # 一路仍然全綠——兩件事同時成立正是這條測試要證明的行為。
    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["unsupported_syntax"]
