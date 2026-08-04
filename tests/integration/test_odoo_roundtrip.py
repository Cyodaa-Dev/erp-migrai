"""對一台真的 Odoo 17 CE 跑完 scan → map → load → verify 四段管線。

**這是整個計劃唯一能證明前面十一個任務真的有用的測試檔。** 前面每一個
CLI 契約測試（`test_cli_load.py`／`test_cli_verify.py`）都刻意把「真的
連上 Odoo 會怎樣」留給這裡（見兩份檔案各自的模組 docstring「誠實界線」
一節）。

⚠️ 誠實界線：這四條測試需要一台可連線的 Odoo（見 `odoo_conn` fixture 讀
的四個環境變數）。CI 的 `integration` job（`.github/workflows/ci.yml`）
會起一台 Odoo 17 CE 並設好這些環境變數；本機沒有 Odoo 時，這裡選擇讓
測試**失敗（`pytest.fail`）而不是跳過**——`make test` 已經用 pytest 的
`-m 'not integration'` 把這個檔案整個濾掉（不會被收集執行），但
`make test-all` 存在的目的就是「真的收集到、真的跑」，若在沒有 Odoo 的
本機用 `skip` 蓋過去，CI 上一旦環境變數設定出錯，也會一併變成假的
綠燈——`skip` 與「沒有這個檢查」在這裡是等價的。

四條測試缺一不可，理由寫在各自的 docstring 裡：
- ①非零資料 + 驗證報告全綠：最基本的「東西真的進得去 Odoo」。
- ②連跑第二次的冪等：spec §4.2「重跑不會建出第二份」唯一的守衛，
  冪等如果只跑一次，就是一個什麼都沒證明的綠燈。
- ③故意改壞一筆來源資料的對照組：沒有這一條，①②的綠燈證明不了任何
  事——一個永遠亮綠的檢查與沒有檢查是等價的。
- ④不支援語法與驗證全綠同時成立：總設計 §3.2 的核心宣稱，必須是一條
  測試而不是一句宣稱。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from erp_migrai.cli import app
from erp_migrai.tools.build_corpus import build_case

runner = CliRunner()

_CASE03 = "case03_sales_order"

# --------------------------------------------------------------------------
# Odoo 連線資訊：`MIGRAI_ODOO_PASSWORD` 是 `migrai load`／`migrai verify`
# 既有的密碼環境變數（P3-2，模組不在這裡發明新的密碼傳遞方式）；另外三個
# （URL／DB／USER）只有這份測試檔與 `make demo-full` 用得到，CLI 本身
# 一律吃 `--odoo-url`／`--odoo-db`／`--odoo-user` 這三個選項，不讀環境
# 變數——這裡讀環境變數只是測試／CI 這一層用來組出那三個選項的值。
# --------------------------------------------------------------------------
_URL_ENV = "MIGRAI_ODOO_URL"
_DB_ENV = "MIGRAI_ODOO_DB"
_USER_ENV = "MIGRAI_ODOO_USER"
_PASSWORD_ENV = "MIGRAI_ODOO_PASSWORD"


@pytest.fixture
def odoo_conn() -> dict[str, str]:
    """讀取連線用的四個環境變數；缺任何一個都是 `pytest.fail`——不是
    `skip`（見模組 docstring 的誠實界線）。"""
    missing = [
        name for name in (_URL_ENV, _DB_ENV, _USER_ENV, _PASSWORD_ENV) if not os.environ.get(name)
    ]
    if missing:
        pytest.fail(
            "整合測試需要一台可連線的 Odoo 17 CE，缺少環境變數："
            f"{'、'.join(missing)}。請跑 CI 的 integration job，或本機自行起一台 "
            "Odoo（`-i base,account,sale,product --without-demo=all`）後設定這些"
            "環境變數再用 `make test-all` 執行；不可以用 skip 蓋過去（那會讓 CI 上"
            "環境變數設錯時也一起變成假綠燈）。"
        )
    return {
        "url": os.environ[_URL_ENV],
        "db": os.environ[_DB_ENV],
        "user": os.environ[_USER_ENV],
    }


@pytest.fixture
def case03_source(tmp_path: Path, corpus_root: Path) -> Path:
    """在 `tmp_path` 現場建一份 case03 的 `data.sqlite`（precedent：
    `tests/test_cli_load.py` 的同名 fixture）——不依賴 `corpus/` 底下
    先前執行留下的 `data.sqlite`，在乾淨 checkout 上也能通過。"""
    case_dir = tmp_path / "case03"
    (case_dir / "src").mkdir(parents=True)
    (case_dir / "data").mkdir()
    shutil.copy(corpus_root / _CASE03 / "src" / "schema.sql", case_dir / "src" / "schema.sql")
    shutil.copy(corpus_root / _CASE03 / "data" / "seed.sql", case_dir / "data" / "seed.sql")
    build_case(case_dir)
    return case_dir / "data" / "data.sqlite"


def _scan_and_map(corpus_root: Path, out: Path) -> None:
    scan_result = runner.invoke(
        app, ["scan", "--src", str(corpus_root / _CASE03 / "src"), "--out", str(out)]
    )
    assert scan_result.exit_code == 0, scan_result.stdout
    map_result = runner.invoke(
        app, ["map", "--inventory", str(out / "inventory.json"), "--out", str(out)]
    )
    assert map_result.exit_code == 0, map_result.stdout


def _load_args(
    mapping_path: Path, source_path: Path, source_id: str, out: Path, odoo: dict[str, str]
) -> list[str]:
    return [
        "load",
        "--mapping",
        str(mapping_path),
        "--source",
        str(source_path),
        "--source-id",
        source_id,
        "--odoo-url",
        odoo["url"],
        "--odoo-db",
        odoo["db"],
        "--odoo-user",
        odoo["user"],
        "--out",
        str(out),
    ]


def _verify_args(
    mapping_path: Path,
    source_path: Path,
    log_path: Path,
    source_id: str,
    out: Path,
    odoo: dict[str, str],
) -> list[str]:
    return [
        "verify",
        "--mapping",
        str(mapping_path),
        "--source",
        str(source_path),
        "--log",
        str(log_path),
        "--source-id",
        source_id,
        "--odoo-url",
        odoo["url"],
        "--odoo-db",
        odoo["db"],
        "--odoo-user",
        odoo["user"],
        "--out",
        str(out),
    ]


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --------------------------------------------------------------------------
# ①非零資料 + 驗證報告全綠
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_full_pipeline_creates_nonzero_data_in_odoo(
    odoo_conn, case03_source, corpus_root, tmp_path
):
    """完成定義①：Odoo 裡真的有非零資料，驗證報告全綠。"""
    out = tmp_path / "out"
    _scan_and_map(corpus_root, out)
    source_id = "task12-full"

    load_result = runner.invoke(
        app, _load_args(out / "mapping.json", case03_source, source_id, out, odoo_conn)
    )
    assert load_result.exit_code == 0, load_result.stdout
    log_entries = _read_jsonl(out / "migration_log.jsonl")
    assert log_entries
    assert any(e["action"] == "created" for e in log_entries)
    assert not any(e["action"] == "failed" for e in log_entries)

    verify_result = runner.invoke(
        app,
        _verify_args(
            out / "mapping.json",
            case03_source,
            out / "migration_log.jsonl",
            source_id,
            out,
            odoo_conn,
        ),
    )
    assert verify_result.exit_code == 0, verify_result.stdout
    report = json.loads((out / "verification_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["total"] > 0
    assert report["summary"]["failed"] == 0
    assert all(c["status"] == "pass" for c in report["checks"])


# --------------------------------------------------------------------------
# ②連跑第二次的冪等
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_second_run_is_idempotent(odoo_conn, case03_source, corpus_root, tmp_path):
    """完成定義②：連跑第二次，migration_log 全是 updated，Odoo 筆數不變。

    🔴 這條測試是 spec §4.2「重跑不會建出第二份」那句承諾的唯一守衛。
    冪等如果只跑一次，就是一個什麼都沒證明的綠燈。
    """
    out = tmp_path / "out"
    _scan_and_map(corpus_root, out)
    source_id = "task12-idem"
    mapping_path = out / "mapping.json"

    first_load = runner.invoke(
        app, _load_args(mapping_path, case03_source, source_id, out, odoo_conn)
    )
    assert first_load.exit_code == 0, first_load.stdout
    first_entries = _read_jsonl(out / "migration_log.jsonl")
    assert first_entries
    assert any(e["action"] == "created" for e in first_entries)

    first_verify = runner.invoke(
        app,
        _verify_args(
            mapping_path, case03_source, out / "migration_log.jsonl", source_id, out, odoo_conn
        ),
    )
    assert first_verify.exit_code == 0, first_verify.stdout
    first_report = json.loads((out / "verification_report.json").read_text(encoding="utf-8"))

    second_load = runner.invoke(
        app, _load_args(mapping_path, case03_source, source_id, out, odoo_conn)
    )
    assert second_load.exit_code == 0, second_load.stdout
    second_entries = _read_jsonl(out / "migration_log.jsonl")
    assert second_entries
    # 全部都是 updated：沒有任何一筆被誤判成 created（=在 Odoo 裡多建了
    # 一份重複資料）或 failed。
    assert {e["action"] for e in second_entries} == {"updated"}

    second_verify = runner.invoke(
        app,
        _verify_args(
            mapping_path, case03_source, out / "migration_log.jsonl", source_id, out, odoo_conn
        ),
    )
    assert second_verify.exit_code == 0, second_verify.stdout
    second_report = json.loads((out / "verification_report.json").read_text(encoding="utf-8"))

    # Odoo 筆數（與金額）不變：兩次驗證報告的每一項檢查逐一比對完全相同。
    def _key(check: dict) -> tuple[str, str]:
        return (check["kind"], check["source_table"])

    first_by_key = {_key(c): c for c in first_report["checks"]}
    second_by_key = {_key(c): c for c in second_report["checks"]}
    assert first_by_key.keys() == second_by_key.keys()
    for key, first_check in first_by_key.items():
        second_check = second_by_key[key]
        assert first_check["target_count"] == second_check["target_count"]
        assert first_check["target_amount"] == second_check["target_amount"]
        assert second_check["status"] == "pass"


# --------------------------------------------------------------------------
# ③故意改壞一筆來源資料的對照組
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_corrupted_source_row_makes_verification_fail(
    odoo_conn, case03_source, corpus_root, tmp_path
):
    """完成定義③：對照組。故意改壞一筆來源資料，驗證階段必須亮紅並發
    E-5001 或 E-5002。

    沒有這一條，前兩條的綠燈證明不了任何事——一個永遠亮綠的檢查與沒有
    檢查是等價的。
    """
    out = tmp_path / "out"
    _scan_and_map(corpus_root, out)
    source_id = "task12-corrupt"
    mapping_path = out / "mapping.json"

    # 先用未改過的來源正常載入一份乾淨資料到 Odoo。
    load_result = runner.invoke(
        app, _load_args(mapping_path, case03_source, source_id, out, odoo_conn)
    )
    assert load_result.exit_code == 0, load_result.stdout

    # 故意改壞一筆來源資料（在來源的副本上動手腳，不動 Odoo 裡已經寫入
    # 的資料）：O0001 第 1 行單價從 1000.0000 改成 1001.0000。案例
    # README 手算過的明細合計是 26999.99，改動這一行會讓「來源重新算出
    # 來的合計」與「Odoo 裡已經寫入的合計」相差 10（遠超過 0.01 的容差），
    # amount_subtotal 檢查必須 fail、發 E-5002。row_count 不受影響（筆數
    # 沒變，只是內容變壞）——這裡刻意驗證 E-5002 這一支，E-5001 已由
    # `tests/pipeline/test_verify.py`／`test_report.py` 的純函式測試覆蓋
    # 過對調 kind 的情境，這裡不重複驗證兩種 kind，只要其中一種在真 Odoo
    # 上真的被觸發即可。
    corrupted_source = tmp_path / "corrupted.sqlite"
    shutil.copy(case03_source, corrupted_source)
    with closing(sqlite3.connect(corrupted_source)) as con:
        con.execute(
            "UPDATE ord_deta SET ord_price = ord_price + 1 WHERE ord_no = 'O0001' AND ord_seq = 1"
        )
        con.commit()

    verify_result = runner.invoke(
        app,
        _verify_args(
            mapping_path,
            corrupted_source,
            out / "migration_log.jsonl",
            source_id,
            out,
            odoo_conn,
        ),
    )
    assert verify_result.exit_code == 1, verify_result.stdout

    report = json.loads((out / "verification_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["failed"] > 0
    failed_kinds = {c["kind"] for c in report["checks"] if c["status"] == "fail"}
    assert failed_kinds & {"row_count", "amount_subtotal"}

    events = _read_jsonl(out / "events.jsonl")
    fired_codes = {e.get("error_code") for e in events}
    assert fired_codes & {"E-5001", "E-5002"}


# --------------------------------------------------------------------------
# ④不支援語法與驗證全綠同時成立
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_unsupported_syntax_coexists_with_green_verification(
    odoo_conn, case03_source, corpus_root, tmp_path
):
    """完成定義④：case03 的 inventory 有 unsupported_syntax，而驗證報告
    仍全綠。資料遷移吃的是 schema.sql 與 data.sqlite，不是 .4gl。

    這是總設計 §3.2 的核心宣稱，必須是一條測試而不是一句宣稱。
    """
    out = tmp_path / "out"
    _scan_and_map(corpus_root, out)

    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["unsupported_syntax"]  # 前提：case03 真的含不支援語法

    source_id = "task12-unsupported"
    mapping_path = out / "mapping.json"

    load_result = runner.invoke(
        app, _load_args(mapping_path, case03_source, source_id, out, odoo_conn)
    )
    assert load_result.exit_code == 0, load_result.stdout

    verify_result = runner.invoke(
        app,
        _verify_args(
            mapping_path, case03_source, out / "migration_log.jsonl", source_id, out, odoo_conn
        ),
    )
    assert verify_result.exit_code == 0, verify_result.stdout
    report = json.loads((out / "verification_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["total"] > 0
    assert report["summary"]["failed"] == 0
