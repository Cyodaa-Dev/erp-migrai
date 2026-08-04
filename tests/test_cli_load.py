"""`migrai load` 的 CLI 契約：exit code、密碼來源、雙軌輸出。

跟 `test_cli_map.py`（map 的 CLI 契約）刻意保持相同的結構與訊息風格。

⚠️ 誠實界線：這裡只測 `--dry-run`（不連 Odoo）與 CLI 本身的參數／錯誤處理
（缺檔案、缺密碼、缺必要選項……）。「真的連上 Odoo 會怎樣」一律留給
Task 12 的 CI 整合測試。

**唯一的例外**（Task 10 審查修正輪次 1 Important I4）：密碼外洩測試指到
`http://127.0.0.1:1`（loopback，沒有任何服務監聽，連線立刻被拒絕，不是
「連上真 Odoo」）——目的不是驗證連線成功後的行為，是驗證連線**注定失敗**
的路徑上密碼有沒有不小心被印出來或序列化進輸出檔案，這件事只有真的走到
建構 `OdooClient` 並嘗試連線才測得到，純 `--dry-run` 覆蓋不到這條路徑。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

import pytest
from typer.testing import CliRunner

from erp_migrai.cli import app
from erp_migrai.pipeline.planner import build_external_id_module_prefix, build_external_id_prefix

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
    transform_error_source, tmp_path
):
    """審查修正輪次 1 Important I1：`migration_log.jsonl` 不帶時間戳的
    整個設計建立在「用 `run_id` join `events.jsonl`」這個承諾上（Task
    11／12 都要靠它）。原本這條測試用的是一個乾淨的 dry-run（不觸發任何
    事件），`events == [] or all(...)` 在空集合上恆為真，`run_id` 從頭到
    尾沒有被兩邊比對過一次（mutation：把 `run_id = event_log.run_id` 換
    成常數字串，這條測試依然 GREEN）。這裡改用會觸發 E-4002 的 fixture，
    確保 `events.jsonl` 真的非空，兩邊的 `run_id` 集合才有東西可比。
    """
    mapping_path, db_path = transform_error_source
    out = tmp_path / "out"
    runner.invoke(app, _dry_run_args(mapping_path, db_path, out))

    log_entries = [
        json.loads(line)
        for line in (out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    events = [
        json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert log_entries
    assert events  # 這次真的觸發了 E-4002，不是空集合——見上方 docstring
    log_run_ids = {e["run_id"] for e in log_entries}
    event_run_ids = {e["run_id"] for e in events}
    assert log_run_ids == event_run_ids
    assert len(log_run_ids) == 1


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
    """審查修正輪次 1 Minor M2：`urlparse(None)` 不拋例外，所以就算把
    `if missing:` 這個檢查整個關掉（mutation：`if False:`），`_endpoint()`
    仍然會因為 `odoo_url=None` 拋 `OdooError`，全部記錄一樣 failed、一樣
    exit 1、一樣沒有 Traceback——這條測試原本只驗證這三件事，通過了但
    守的不是它宣稱的東西（Task 6 撞過的形狀）。加一條斷言指名訊息真的
    點出 `--odoo-url`，才能分辨「真的被這個檢查擋下」與「繞過檢查、
    在別的地方碰巧也失敗」。"""
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
    assert "--odoo-url" in result.stdout


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


@pytest.fixture
def transform_error_source(tmp_path):
    """一份會觸發 E-4002 的最小來源＋mapping：`flag` 欄位的值 `'X'` 不在
    `value_map` 裡也沒有 `default`。同時被
    `test_transform_error_in_dry_run_still_exits_one`（純粹驗證 E-4002
    契約）與 I1 的 `run_id` join 測試（需要至少一則事件才能驗證 join
    關係，見該測試 docstring）共用，避免兩處各自維護一份幾乎相同的
    fixture。"""
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
    return mapping_path, db_path


def test_transform_error_in_dry_run_still_exits_one(transform_error_source, tmp_path):
    """`dry_run` 不會壓下 E-4002：轉換失敗是資料本身的問題，跟有沒有連
    Odoo 無關，`--dry-run` 存在的目的正是要讓貢獻者能在本機發現這種
    問題。"""
    mapping_path, db_path = transform_error_source
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
    #
    # 審查修正輪次 1 Important I3：任務書 Step 3 原本給的斷言是
    # `.startswith("migrai.")`——任何 `--source-id` 都能通過，`--source-id`
    # 打錯字（Odoo 那邊會全部落在錯誤的 external_id 底下，重跑不會變成
    # update 而是全部新建一份重複資料，只有真 Odoo 才看得出來）不會被這
    # 條測試發現（mutation：`source_id="fixed"` 寫死，這條測試依然
    # GREEN）。這裡改成比對完整字串，把 `--source-id="case03"` 真的有沒
    # 有被用上釘死。
    ord_entry = next(e for e in entries if e["source_table"] == "ord_mast")
    assert ord_entry["external_id"] == "migrai.case03.ord_mast.O0001"

    # spec §4：case03 的 .4gl 刻意含不支援語法（scan 的 inventory.json 會有
    # unsupported_syntax），但資料遷移吃的是 schema.sql／data.sqlite，這裡
    # 一路仍然全綠——兩件事同時成立正是這條測試要證明的行為。
    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["unsupported_syntax"]


# --------------------------------------------------------------------------
# 審查修正輪次 1 Important I4：密碼不能外洩到 stdout／events.jsonl／
# migration_log.jsonl
# --------------------------------------------------------------------------


def test_password_env_var_never_leaks_and_is_actually_read(
    minimal_mapping_path, minimal_source, tmp_path, monkeypatch
):
    """現況實作是乾淨的（`--help` 無 `--password `、`OdooClient` 沒被序列化
    進事件），但這件事原本沒有任何測試守衛——`odoo_client.py` 的
    docstring 已經明講「未封住、呼叫端自己要避開」的路徑就是「把整個
    `OdooClient` 序列化進事件或 log」，`migrai load` 正是那個呼叫端。

    `--odoo-url http://127.0.0.1:1` 是 loopback 且沒有服務監聽，連線必定
    立刻被拒絕——不是在測「連上真 Odoo 會怎樣」，是測「連線注定失敗的
    這條路徑上，密碼有沒有不小心流進使用者看得到或寫進磁碟的地方」。

    順便守住 I4 的第二部分：`MIGRAI_ODOO_PASSWORD` 這個環境變數名稱真的
    有被讀取——如果它被忽略（例如改名後沒改對應的讀取邏輯），程式會停在
    「未提供密碼」那個分支，走不到真正嘗試連線、進而失敗的分支。
    """
    secret = "sup3rs3cret"  # noqa: S105 - 測試用假密碼，不是真的密鑰
    monkeypatch.setenv("MIGRAI_ODOO_PASSWORD", secret)
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
            "http://127.0.0.1:1",
            "--odoo-db",
            "migrai",
            "--odoo-user",
            "admin",
            "--out",
            str(out),
        ],
    )

    assert secret not in result.stdout
    # 若「未提供密碼」，代表 MIGRAI_ODOO_PASSWORD 根本沒被讀到，走到了
    # 錯的分支——而不是真的嘗試連線後失敗。
    assert "未提供密碼" not in result.stdout
    assert result.exit_code == 1

    events_text = (out / "events.jsonl").read_text(encoding="utf-8")
    assert secret not in events_text

    log_path = out / "migration_log.jsonl"
    if log_path.is_file():
        assert secret not in log_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 審查修正輪次 1 Important I5／I9：E-4001 中止時 stdout 不能仍然印綠勾，
# 且 UnsupportedReference 不能逃出去變成裸例外
# --------------------------------------------------------------------------


def test_e4001_abort_prints_failure_symbol_and_names_the_reason(tmp_path):
    """實跑證據：來源表沒有主鍵 → `UnsupportedReference` → E-4001 中止，
    但原本 stdout 仍然印 `✓ 載入完成：共 0 筆（...）`，原因只寫在
    `events.jsonl` 裡——本機沒有 Odoo 的貢獻者只看得到這個畫面，看到綠勾
    會誤以為真的成功了（`cli.scan`／`cli.map` 失敗時都印 ✗，只有
    `load` 沒有）。

    同時這個「沒有主鍵的表」場景也驗證了 I9：`UnsupportedReference` 是
    `Exception` 的直接子類（不是 `ValueError`），CLI 的
    `except (LogEntrySchemaError, ValueError)` 接不住——`run_load` 自己
    要把它收斂住，不能讓它逃出去變成使用者看到的裸 Traceback。
    """
    db_path = tmp_path / "data.sqlite"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE cust (id CHAR(4), name CHAR(20))")  # 刻意無 PRIMARY KEY
        con.execute("INSERT INTO cust VALUES ('C001', 'Alice')")
        con.commit()
    out = tmp_path / "out"
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(_MINIMAL_MAPPING), encoding="utf-8")

    result = runner.invoke(app, _dry_run_args(mapping_path, db_path, out))

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert "✗" in result.stdout
    assert "✓ 載入完成" not in result.stdout
    assert "cust" in result.stdout  # 原因裡點名是哪張表

    events = [
        json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["error_code"] == "E-4001" for e in events)
    assert (out / "migration_log.jsonl").read_text(encoding="utf-8") == ""


# --------------------------------------------------------------------------
# 最終整分支審查 Critical C1：`verify` 組前綴要跟 `load` 寫出的
# external_id 用同一套編碼
# --------------------------------------------------------------------------


def test_c1_external_id_prefix_helpers_match_load_output_for_hyphenated_source_id(
    minimal_mapping_path, minimal_source, tmp_path
):
    """回歸測試：`planner.build_external_id_prefix`／
    `build_external_id_module_prefix`（`verify` 側用來比對 Odoo／log 的
    前綴）必須能對上 `load`（`planner.build_external_id`）實際寫進
    `migration_log.jsonl` 的 `external_id`。

    🔴 刻意不用 `case03` 當夾具：`encode_pk_value("case03") == "case03"`
    （只含 `[A-Za-z0-9_]`），編碼前後看起來一模一樣，這條測試若用它就是
    空轉的——連原本的 bug（裸 f-string 沒過 `encode_pk_value`）都測不出來。
    這裡改用 `task12-full`：CI 整合測試（`tests/integration/
    test_odoo_roundtrip.py`）四條測試真正在用的 `source_id`，也是最終
    整分支審查實跑抓到「`verify` 找不到任何一筆」這個缺陷時用的值。

    純字串比對：`load --dry-run` 不連 Odoo，這裡也不用起任何假 server。
    """
    out = tmp_path / "out"
    source_id = "task12-full"

    result = runner.invoke(
        app,
        [
            "load",
            "--mapping",
            str(minimal_mapping_path),
            "--source",
            str(minimal_source),
            "--source-id",
            source_id,
            "--out",
            str(out),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0

    entries = [
        json.loads(line)
        for line in (out / "migration_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert entries, "沒有任何一筆 migration_log 記錄，這條測試沒有東西可以比對"
    entry = entries[0]
    assert entry["source_table"] == "cust"

    # cli._resolve_source_counts 場景之外的另一半：cli.verify_command 讀
    # --log 用的前綴（比對「完整 external_id」，含 migrai. 這段 module）。
    module_prefix = build_external_id_module_prefix(source_id)
    assert entry["external_id"].startswith(module_prefix), (
        f"{entry['external_id']!r} 對不上 module 前綴 {module_prefix!r}——"
        "cli.verify_command 的 --log 守衛會誤判合法的 log 為與 --source-id 不符"
    )

    # verify.resolve_target_counts／cli._target_line_amount 場景：比對的
    # 對象是 ir.model.data 的 name 欄位，Odoo 已經把 module 段拆掉了，
    # 所以這裡比對時也要先把 "migrai." 去掉再比。
    name_without_module = entry["external_id"][len("migrai.") :]
    table_prefix = build_external_id_prefix(source_id, entry["source_table"])
    assert name_without_module.startswith(table_prefix), (
        f"{name_without_module!r} 對不上表前綴 {table_prefix!r}——"
        "verify 會把這張表的 target_count 誤算成 0，對每張表發出不存在的 E-5001"
    )
