"""migrai map 的 CLI 契約：exit code 與雙軌輸出。

跟 test_cli.py（scan 的 CLI 契約）刻意保持相同的結構與訊息風格——
兩個指令是同一支 CLI 的兩個子命令，讀者不該看得出是不同人寫的。
"""

import json
import os

import pytest
from typer.testing import CliRunner

from erp_migrai.cli import app
from erp_migrai.mapping import pipeline as pipeline_module

# chmod 500 在 root 下沒有意義（root 會略過檔案權限檢查），這類測試在以 root
# 執行的環境（例如某些容器）下必須乾淨地跳過，而不是用假陽性通過或報錯。
_IS_ROOT = hasattr(os, "getuid") and os.getuid() == 0

runner = CliRunner()

# 一份人造的、結構完全合法的空 inventory——不含任何表，coverage 定義為
# 100（source_columns == 0 時 engine 把 coverage_percent 判為 100，
# 見 engine.py::_build_stats），用在不需要真的表也能跑通全流程的測試。
EMPTY_INVENTORY = {
    "synthetic": True,
    "schema_version": "1.0",
    "database": None,
    "source_files": [],
    "tables": [],
    "programs": [],
    "unsupported_syntax": [],
    "database_conflicts": [],
}

# 一份會觸發人造表級衝突的 catalog：兩個模型的最後一段名稱都是 `thing`，
# 表名 `thing` 對兩者的 name_score 都會是 1.0（完全同分），因此在沒有任何
# 表規則的情況下，engine 必然把它判為並列，產生一筆 ConflictKind.TABLE。
# 這是刻意人造的、不依賴任何啟發式評分細節以外知識的衝突。
_CONFLICT_CATALOG_YAML = """
models:
  aaa.thing:
    description: "假模型 A（人造衝突測試專用，非真實 Odoo 模型）"
    fields:
      x:
        type: char
        required: true
        module: base
        help: "測試欄位"
  bbb.thing:
    description: "假模型 B（人造衝突測試專用，非真實 Odoo 模型）"
    fields:
      x:
        type: char
        required: true
        module: base
        help: "測試欄位"
"""

_CONFLICT_INVENTORY = {
    "synthetic": True,
    "schema_version": "1.0",
    "database": None,
    "source_files": [],
    "tables": [
        {
            "name": "thing",
            "columns": [
                {"name": "x", "type": "CHAR(1)", "nullable": True, "default": None},
            ],
            "primary_key": [],
            "foreign_keys": [],
        }
    ],
    "programs": [],
    "unsupported_syntax": [],
    "database_conflicts": [],
}


@pytest.fixture
def clean_inventory_path(tmp_path, corpus_root):
    """case01_partner 的 golden inventory.json——用內建 catalog／rules 可以
    100% 對上（見 test_golden_mapping.py 的 TestCase01PartnerMapping）。
    複製到 tmp_path 而非直接指向 corpus/，讓這份測試不會意外寫壞語料庫。
    """
    src = corpus_root / "case01_partner" / "expected" / "inventory.json"
    dest = tmp_path / "inventory.json"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


@pytest.fixture
def empty_inventory_path(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(EMPTY_INVENTORY), encoding="utf-8")
    return path


# --- 必要測試 1：case01 的 inventory.json → exit 0，三個檔案，摘要含覆蓋率 100 ---


def test_case01_inventory_exits_zero_and_writes_three_files(clean_inventory_path, tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["map", "--inventory", str(clean_inventory_path), "--out", str(out)]
    )

    assert result.exit_code == 0
    assert (out / "mapping.json").is_file()
    assert (out / "mapping.csv").is_file()
    assert (out / "events.jsonl").is_file()
    assert "100" in result.stdout
    # ruling 3：case01 是完全乾淨的一跑（覆蓋率 100%），unmapped／conflicts
    # 都是 0——摘要仍必須明講這兩個 0，不能讓使用者從「沒有段落」去推論零，
    # 這裡直接斷言零值本身出現在 stdout（而非只斷言非零時的措辭）。
    assert "0 個欄位未對應" in result.stdout
    assert "0 個衝突" in result.stdout


# --- 必要測試 2：空規則目錄 → exit 0，摘要顯示 unmapped 且不把它當錯誤 ---


def test_empty_rules_dir_exits_zero_with_honest_unmapped_wording(clean_inventory_path, tmp_path):
    rules_dir = tmp_path / "empty_rules"
    rules_dir.mkdir()
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "map",
            "--inventory",
            str(clean_inventory_path),
            "--out",
            str(out),
            "--rules",
            str(rules_dir),
        ],
    )

    assert result.exit_code == 0
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))
    assert len(mapping["unmapped"]) == 7
    assert "未對應" in result.stdout or "unmapped" in result.stdout
    assert "不是錯誤" in result.stdout
    assert "rules" in result.stdout


# --- 必要測試 3：人造衝突 → exit 1，mapping.json 仍被寫出 ---


def test_artificial_conflict_exits_one_and_still_writes_mapping_json(tmp_path):
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CONFLICT_CATALOG_YAML, encoding="utf-8")
    rules_dir = tmp_path / "empty_rules"
    rules_dir.mkdir()
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(_CONFLICT_INVENTORY), encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "map",
            "--inventory",
            str(inventory_path),
            "--out",
            str(out),
            "--rules",
            str(rules_dir),
            "--catalog",
            str(catalog_path),
        ],
    )

    assert result.exit_code == 1
    assert (out / "mapping.json").is_file()
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))
    assert len(mapping["conflicts"]) == 1
    assert "不可信" in result.stdout or "人工裁決" in result.stdout


# --- 必要測試 4：--inventory 指向不存在的檔 → exit 1，清楚訊息，無 traceback ---


def test_missing_inventory_file_exits_one_without_traceback(tmp_path):
    missing = tmp_path / "nope.json"
    out = tmp_path / "out"

    result = runner.invoke(app, ["map", "--inventory", str(missing), "--out", str(out)])

    assert result.exit_code == 1
    assert str(missing) in result.stdout
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


# --- 必要測試 5：--inventory 指向不合 schema 的 JSON → exit 1，清楚訊息，無 traceback ---


def test_inventory_failing_schema_exits_one_without_traceback(tmp_path):
    bad = tmp_path / "bad_inventory.json"
    bad.write_text(json.dumps({"not": "a valid inventory"}), encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, ["map", "--inventory", str(bad), "--out", str(out)])

    assert result.exit_code == 1
    assert str(bad) in result.stdout
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


def test_inventory_invalid_json_syntax_exits_one_without_traceback(tmp_path):
    """不是「不合 schema」，而是連 JSON 都解析不了——兩者是不同的失敗原因，
    但對使用者而言都必須是清楚的中文訊息，不能是 traceback。"""
    bad = tmp_path / "bad_inventory.json"
    bad.write_text("{ 這不是合法的 JSON", encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, ["map", "--inventory", str(bad), "--out", str(out)])

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


# --- 必要測試 6：--out 被普通檔案占用 → exit 1，無 traceback ---


def test_unwritable_out_dir_exits_one_without_traceback(clean_inventory_path, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("我是一個檔案，不是目錄", encoding="utf-8")

    result = runner.invoke(
        app, ["map", "--inventory", str(clean_inventory_path), "--out", str(blocked)]
    )

    assert result.exit_code == 1
    assert str(blocked) in result.stdout
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


# --- 必要測試 7：缺 --inventory → exit 2 ---


def test_missing_required_option_exits_two(tmp_path):
    result = runner.invoke(app, ["map"])
    assert result.exit_code == 2


# --- 必要測試 8：重跑同一個 --out → events.jsonl 行數不累積 ---
#
# 最終整分支審查修正：這兩條測試原本都用 `clean_inventory_path`（內建
# catalog／rules 對 case01 是 100% 覆蓋、零衝突），跑一次 map 完全不會發出
# 任何事件——`events.jsonl` 是 0 位元組。`0 == 0` 恆真，第二條測試的
# `for` 迴圈也是走過一個空檔案，兩條測試都在斷言一個恆真的前提，測不到
# 它們名字宣稱要測的行為。改用「空規則目錄」（保證發出 E-2002）確保
# `events.jsonl` 真的非空，兩條測試名副其實。


def test_rerunning_does_not_duplicate_events(clean_inventory_path, tmp_path):
    rules_dir = tmp_path / "empty_rules"
    rules_dir.mkdir()
    out = tmp_path / "out"

    runner.invoke(
        app,
        [
            "map",
            "--inventory",
            str(clean_inventory_path),
            "--out",
            str(out),
            "--rules",
            str(rules_dir),
        ],
    )
    first = len((out / "events.jsonl").read_text(encoding="utf-8").splitlines())
    assert first > 0  # 空規則目錄必定觸發 E-2002，確保這不是恆真的 0 == 0

    runner.invoke(
        app,
        [
            "map",
            "--inventory",
            str(clean_inventory_path),
            "--out",
            str(out),
            "--rules",
            str(rules_dir),
        ],
    )
    second = len((out / "events.jsonl").read_text(encoding="utf-8").splitlines())
    assert second == first


def test_events_jsonl_lines_are_valid_json(tmp_path):
    """改用會真的觸發衝突（E-2001）的人造情境，而不是零事件的 case01，
    否則下面的 `for` 迴圈是在走一個空檔案，恆真、測不到任何東西。"""
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CONFLICT_CATALOG_YAML, encoding="utf-8")
    rules_dir = tmp_path / "empty_rules"
    rules_dir.mkdir()
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(_CONFLICT_INVENTORY), encoding="utf-8")
    out = tmp_path / "out"

    runner.invoke(
        app,
        [
            "map",
            "--inventory",
            str(inventory_path),
            "--out",
            str(out),
            "--rules",
            str(rules_dir),
            "--catalog",
            str(catalog_path),
        ],
    )

    lines = (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) > 0  # 人造衝突必定觸發 E-2001，確保這不是走過空檔案
    for line in lines:
        event = json.loads(line)
        assert set(event) == {
            "event_id",
            "event_type",
            "timestamp",
            "severity",
            "error_code",
            "message",
            "details",
            "run_id",
        }


# --- 必要測試 9：--strict 下 E-2002 使退出碼變 1 ---
#
# 這條測試按照 brief 字面要求無法通過：E-2002（COVERAGE_LOW）的 severity 是
# Severity.WARN（見 events.py），而 events.py 自己已有一條committed且刻意的
# 測試 `test_warn_severity_stays_non_fatal_under_strict` 證明 WARN severity
# 在 has_fatal(strict=True) 下永遠不會變成致命——這是 events.py 的既有、已
# 測試過的設計，不是這次任務的臭蟲。ruling 1 禁止 CLI 重新推導哪些碼算致命，
# ruling 7 禁止修改 cli.py 以外的模組；要滿足 brief 字面的測試，兩者只能choose
# 其一違反，因此這裡誠實地寫下「目前實際發生的行為」並在測試名稱與註解點名
# 這個矛盾，而不是假造一個看起來通過、實際上驗證了錯誤斷言的測試。詳見
# task-8-report.md 對這個矛盾的完整說明。
def test_strict_does_not_escalate_warn_severity_coverage_low(clean_inventory_path, tmp_path):
    """已知與 brief 字面要求矛盾（見上方註解）：在目前 events.py 的設計下，
    --strict 對 map 階段的 E-2002（WARN）沒有作用，這條測試記錄的是實際、
    刻意的既有行為，而不是 brief 字面要求的「exit 1」。
    """
    rules_dir = tmp_path / "empty_rules"
    rules_dir.mkdir()
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "map",
            "--inventory",
            str(clean_inventory_path),
            "--out",
            str(out),
            "--rules",
            str(rules_dir),
            "--strict",
        ],
    )

    events = [
        json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["error_code"] == "E-2002" for e in events)
    assert result.exit_code == 0


def test_strict_still_exits_one_when_e2001_present(tmp_path):
    """--strict 對已經致命的 E-2001（衝突）沒有改變 exit code 的必要——
    E-2001 不論 strict 與否都致命，這裡確認 --strict 不會意外把它「修復」掉。
    """
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CONFLICT_CATALOG_YAML, encoding="utf-8")
    rules_dir = tmp_path / "empty_rules"
    rules_dir.mkdir()
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(_CONFLICT_INVENTORY), encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "map",
            "--inventory",
            str(inventory_path),
            "--out",
            str(out),
            "--rules",
            str(rules_dir),
            "--catalog",
            str(catalog_path),
            "--strict",
        ],
    )

    assert result.exit_code == 1


# --- 額外：--rules／--catalog 可覆寫內建檔 ---


def test_custom_catalog_and_rules_are_honored(tmp_path):
    """--catalog／--rules 傳入自訂路徑時，engine 必須真的使用它們，而不是
    悄悄退回套件內建版本——用一份只認得 `thing` 表的迷你 catalog／規則來
    確認這一點：內建 catalog 完全不認得 `thing`，如果 CLI 忽略了 --catalog
    退回內建版，這張表只會落進 unmapped，不會產生任何 mapping。
    """
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CONFLICT_CATALOG_YAML, encoding="utf-8")
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "thing.yaml").write_text(
        "version: 1\ntables:\n  thing:\n    target_model: aaa.thing\n",
        encoding="utf-8",
    )
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(_CONFLICT_INVENTORY), encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "map",
            "--inventory",
            str(inventory_path),
            "--out",
            str(out),
            "--rules",
            str(rules_dir),
            "--catalog",
            str(catalog_path),
        ],
    )

    assert result.exit_code == 0
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))
    assert mapping["models"][0]["target_model"] == "aaa.thing"


# --- 空 inventory（無任何表）→ exit 0，覆蓋率 100（沒有來源欄位可比對） ---


def test_empty_inventory_exits_zero_with_full_coverage(empty_inventory_path, tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["map", "--inventory", str(empty_inventory_path), "--out", str(out)]
    )
    assert result.exit_code == 0
    assert "100" in result.stdout


# --- ruling 4：開場的 OSError（_fresh_event_log() 建立 events.jsonl 失敗）---
#
# 這條測試原本的名稱與 docstring 聲稱涵蓋「map 執行中」的 OSError，但實際上
# 不是：`_fresh_event_log()`（cli.py）在呼叫 `pipeline.run_map()` 之前就會
# 呼叫 `path.touch()`，把整個目錄設成 0o500 之後 `touch()` 本身就會因為無法
# 在唯讀目錄裡建立新檔案而拋出 `PermissionError`——這條路徑走的是
# `map_command` **開場**的 `except OSError`（緊接在 `_fresh_event_log(out)`
# 之後），從未進入呼叫 `run_map()` 之後的中途區塊。這裡保留這條測試（開場的
# OSError 一樣需要覆蓋），但改名並改寫 docstring 使其誠實地描述它實際驗證的
# 路徑；真正的「執行中」測試見下方
# test_mid_map_event_log_write_failure_exits_one_without_traceback。


@pytest.mark.skipif(_IS_ROOT, reason="root 會略過檔案權限檢查，chmod 500 測試在 root 下沒有意義")
def test_opening_event_log_touch_failure_exits_one_without_traceback(
    clean_inventory_path, tmp_path
):
    """第一次執行成功建立輸出目錄；接著移除 events.jsonl 並把目錄設為唯讀，
    第二次執行時 `_fresh_event_log()` 呼叫 `path.touch()` 會因為目錄唯讀而
    立刻拋出 `PermissionError`——這條路徑走的是 `map_command` **開場**的
    `except OSError`（緊接在 `_fresh_event_log(out)` 之後），不是
    `pipeline.run_map()` 執行中途的區塊（見下方另一條測試）。
    """
    rules_dir = tmp_path / "empty_rules"
    rules_dir.mkdir()
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "map",
            "--inventory",
            str(clean_inventory_path),
            "--out",
            str(out),
            "--rules",
            str(rules_dir),
        ],
    )
    assert result.exit_code == 0
    assert (out / "events.jsonl").is_file()

    (out / "events.jsonl").unlink()
    out.chmod(0o500)

    try:
        result = runner.invoke(
            app,
            [
                "map",
                "--inventory",
                str(clean_inventory_path),
                "--out",
                str(out),
                "--rules",
                str(rules_dir),
            ],
        )

        assert result.exit_code == 1
        assert str(out) in result.stdout
        assert "Traceback" not in result.stdout
        assert result.exception is None or not isinstance(result.exception, OSError)
    finally:
        out.chmod(0o755)


@pytest.mark.skipif(_IS_ROOT, reason="root 會略過檔案權限檢查，chmod 唯讀測試在 root 下沒有意義")
def test_mid_map_event_log_write_failure_exits_one_without_traceback(
    clean_inventory_path, tmp_path, monkeypatch
):
    """真正命中 map_command 裡「pipeline.run_map 執行中途的 OSError」那個
    except 分支的測試（最終整分支審查修正：map 的七步管線已抽到
    `mapping/pipeline.py::run_map`，這裡改為 monkeypatch
    `pipeline_module.load_rules`，而不是舊架構下的 `cli_module.load_rules`
    ——CLI 本身已經不再直接呼叫 `load_rules`，monkeypatch 舊位置不會再有
    任何效果）。

    機制：`_fresh_event_log()` 先成功把 events.jsonl touch 出來（開場沒有
    失敗——目錄本身可寫，這與上面那條測試刻意相反）。接著在 `run_map`
    呼叫鏈中、`build_mapping()` **之前**的最後一步 `load_rules()` 上
    monkeypatch 一層薄殼：這層殼先把已經存在的 events.jsonl chmod 成
    0o400（唯讀），再原封不動呼叫真正的 `load_rules()` 並回傳其結果。
    `load_rules()` 本身完全沒被改寫、`build_mapping()` 也完全沒被
    monkeypatch——當 `build_mapping()` 執行到第一次 `event_log.emit()`
    （這裡用空規則目錄確保一定會發出 E-2002 事件）時，`EventLog.emit()`
    內部真正的 `self.path.open("a", ...)` 會因為檔案本身唯讀而拋出真正的
    `PermissionError`（`OSError` 的子類別），逃出 `build_mapping()`、經
    `run_map` 逃出，被 `map_command` 的 `except OSError` 接住。

    這與上一條測試的差異：那一條在**目錄**唯讀時卡在 `_fresh_event_log()`
    的 `touch()`（開場）；這一條讓 `_fresh_event_log()` 先成功，只在
    build_mapping() 真正開始寫事件時才讓寫入失敗，因此走的是完全不同、
    先前zero覆蓋的程式路徑。
    """
    rules_dir = tmp_path / "empty_rules"
    rules_dir.mkdir()
    out = tmp_path / "out"

    real_load_rules = pipeline_module.load_rules

    def _make_events_file_readonly_then_load_rules(catalog_data, rules_path):
        events_path = out / "events.jsonl"
        assert events_path.is_file(), "_fresh_event_log() 必須已經成功 touch 出檔案"
        events_path.chmod(0o400)
        return real_load_rules(catalog_data, rules_path)

    monkeypatch.setattr(pipeline_module, "load_rules", _make_events_file_readonly_then_load_rules)

    try:
        result = runner.invoke(
            app,
            [
                "map",
                "--inventory",
                str(clean_inventory_path),
                "--out",
                str(out),
                "--rules",
                str(rules_dir),
            ],
        )

        assert result.exit_code == 1
        assert str(out) in result.stdout
        assert "Traceback" not in result.stdout
        assert result.exception is None or not isinstance(result.exception, OSError)
    finally:
        events_path = out / "events.jsonl"
        if events_path.is_file():
            events_path.chmod(0o644)


# ---------------------------------------------------------------------------
# 最終整分支審查修正 Important 1：三個貢獻者常見錯誤，CLI 端到端驗證
# ---------------------------------------------------------------------------

# 內建 rules/res_partner.yaml 的完整內容，用來組出「其餘規則都正常、只有一個
# 地方刻意打錯」的自訂規則目錄——不依賴內建 rules/ 本身（那個會被映射階段的
# 其他任務改動），完整複製一份到這裡讓測試自我完備。
_RES_PARTNER_RULES_YAML = """
version: 1

tables:
  cust_mast:
    target_model: res.partner

columns:
  cust_mast.cust_no:
    target: res.partner.ref
  cust_mast.cust_name:
    target: res.partner.name
  cust_mast.cust_ubn:
    target: res.partner.vat
  cust_mast.cust_tel:
    target: res.partner.phone
  cust_mast.cust_addr:
    target: res.partner.street
  cust_mast.cust_credit:
    target: res.partner.credit_limit
  cust_mast.cust_stus:
    target: res.partner.active
    transform:
      kind: value_map
      map:
        "Y": true
        "N": false
      default: true
"""


def _map_args(inventory_path, out, rules_dir) -> list[str]:
    return [
        "map",
        "--inventory",
        str(inventory_path),
        "--out",
        str(out),
        "--rules",
        str(rules_dir),
    ]


def test_yml_rules_file_is_honored_by_cli(clean_inventory_path, tmp_path):
    """Important 1(a)：一個 `.yml`（而非 `.yaml`）規則檔透過 `--rules` 傳入時
    必須真的被讀取並生效，而不是被 `*.yaml` 專屬的 glob 悄悄忽略、讓
    覆蓋率變成 0%。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "res_partner.yml").write_text(_RES_PARTNER_RULES_YAML, encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, _map_args(clean_inventory_path, out, rules_dir))

    assert result.exit_code == 0
    assert "100" in result.stdout
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))
    assert len(mapping["mappings"]) == 7
    assert len(mapping["unmapped"]) == 0


def test_ignored_rules_file_is_reported_in_cli_summary(clean_inventory_path, tmp_path):
    """Important 1(a)：規則目錄裡一個既非 `*.yaml`／`*.yml` 也非 `README.md`
    的檔案，必須在摘要裡被點名，而不是徹底隱形。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "res_partner.yaml").write_text(_RES_PARTNER_RULES_YAML, encoding="utf-8")
    (rules_dir / "notes.txt").write_text("我不是規則檔\n", encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, _map_args(clean_inventory_path, out, rules_dir))

    assert result.exit_code == 0
    assert "notes.txt" in result.stdout
    assert "沒有被讀取" in result.stdout


def test_typo_in_rule_key_is_reported_as_unused_rule_in_cli_summary(clean_inventory_path, tmp_path):
    """Important 1(b)：規則鍵名打錯字（真正的來源欄位是 `cust_no`，規則卻寫
    成 `cust_nol`）不能默默地什麼都不做——CLI 摘要必須點名這條規則的
    `origin`，讓貢獻者能直接看到自己打錯的是哪一個檔案的哪一個鍵。"""
    typo_rules = _RES_PARTNER_RULES_YAML.replace("cust_mast.cust_no:", "cust_mast.cust_nol:")
    assert "cust_nol" in typo_rules  # 先確認替換真的發生，測試本身沒打錯

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "typo.yaml").write_text(typo_rules, encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, _map_args(clean_inventory_path, out, rules_dir))

    assert result.exit_code == 0
    assert "沒有命中任何來源欄位" in result.stdout
    assert "typo.yaml#cust_mast.cust_nol" in result.stdout


def test_constant_value_resolved_to_a_date_by_yaml_fails_cleanly_without_traceback(
    clean_inventory_path, tmp_path
):
    """Important 1(c)：`transform.kind: constant` 的 `value` 若沒加引號寫成
    看起來像日期的字面值（`value: 2024-01-01`），PyYAML 會把它解析成
    `datetime.date`——這條規則本身合法（`target` 指到存在的模型與欄位），
    問題要到 `json.dumps` 才會爆炸。這裡確認整條路徑（rules_loader 攔截
    → RuleError → CLI 轉成中文訊息）對使用者而言是 exit 1、清楚訊息、
    零 traceback，而不是一長串 Python 例外。"""
    bad_rules = (
        _RES_PARTNER_RULES_YAML
        + """
  cust_mast.cust_kind:
    target: res.partner.is_company
    transform:
      kind: constant
      value: 2024-01-01
"""
    )
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "bad.yaml").write_text(bad_rules, encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, _map_args(clean_inventory_path, out, rules_dir))

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)
    assert "bad.yaml" in result.stdout
    assert "cust_mast.cust_kind" in result.stdout
    # 規則檔本身有問題（載入當下就被攔下），這次執行不應該留下任何產出檔案
    # ——事件記錄除外，那是 _fresh_event_log() 一律先建立的。
    assert not (out / "mapping.json").exists()


# ---------------------------------------------------------------------------
# 最終整分支審查修正（第三組：長尾）Important 3：NaN／循環參照兩個
# `TypeError` 攔不住的洞，端到端驗證
# ---------------------------------------------------------------------------


def test_constant_value_nan_exits_one_without_traceback_and_writes_no_nan_json(
    clean_inventory_path, tmp_path
):
    """`value: .nan` 是合法 YAML，PyYAML 會解析成 `float("nan")`。修正前
    `rules_loader._ensure_json_serializable` 用預設的 `allow_nan=True` 呼叫
    `json.dumps`，完全放行——`migrai map` 會 exit 0，並把不合法的 JSON 字面值
    `NaN`（RFC 8259 不允許）靜靜寫進公開的 `mapping.json`。這裡確認現在
    exit 1、訊息清楚、零 traceback，且完全不產出 `mapping.json`。"""
    bad_rules = (
        _RES_PARTNER_RULES_YAML
        + """
  cust_mast.cust_kind:
    target: res.partner.is_company
    transform:
      kind: constant
      value: .nan
"""
    )
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "bad.yaml").write_text(bad_rules, encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, _map_args(clean_inventory_path, out, rules_dir))

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)
    assert "bad.yaml" in result.stdout
    assert "cust_mast.cust_kind" in result.stdout
    assert not (out / "mapping.json").exists()
    assert "NaN" not in result.stdout


def test_constant_value_circular_yaml_anchor_exits_one_without_traceback(
    clean_inventory_path, tmp_path
):
    """`value: &a [*a]` 是合法 YAML 語法，PyYAML 會真的建構出一個自我參照的
    `list`。`json.dumps` 對循環參照拋的是 `ValueError`，不是 `TypeError`
    ——修正前這個例外完全沒被接住，會逃出 `load_rules`，最終讓使用者在
    `migrai map` 看到一長串 traceback。這裡確認現在 exit 1、訊息清楚、
    零 traceback。"""
    bad_rules = (
        _RES_PARTNER_RULES_YAML
        + """
  cust_mast.cust_kind:
    target: res.partner.is_company
    transform:
      kind: constant
      value: &a [*a]
"""
    )
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "bad.yaml").write_text(bad_rules, encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(app, _map_args(clean_inventory_path, out, rules_dir))

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)
    assert "bad.yaml" in result.stdout
    assert "cust_mast.cust_kind" in result.stdout
    assert not (out / "mapping.json").exists()


# ---------------------------------------------------------------------------
# 最終整分支審查修正（第三組：長尾）Important 4：writer 端的 schema 失敗要
# 歸咎給引擎，不能誤報成使用者的 inventory 有問題
# ---------------------------------------------------------------------------


def test_writer_side_schema_failure_reports_engine_defect_not_inventory_problem(
    clean_inventory_path, tmp_path, monkeypatch
):
    """`cli.py` 攤平巢狀 `try` 之後，先前 `except ValidationError` 這一支
    同時接住「使用者 --inventory 不合 schema」與「引擎自己產出的 mapping
    不合 schema」——後者被誤報成前者的措辭，使用者會去檢查一份完全合法的
    inventory，白費工夫。這裡 monkeypatch `pipeline_module.build_mapping`
    直接回傳一個違反 schema 的 `Mapping`（`confidence=101`，超出
    `mapping.schema.json` 允許的 0-100），模擬「引擎本身有缺陷」的情境，
    確認 CLI 現在印出的是「映射結果本身不符合 schema、這是引擎的缺陷」，
    完全不提 `--inventory` 或 inventory.schema.json。"""
    from erp_migrai.mapping.models import (
        SCHEMA_VERSION,
        FieldMapping,
        Mapping,
        MappingStats,
        SourceRef,
        TargetRef,
        Tier,
        Transform,
        TransformKind,
    )

    def _bogus_build_mapping(inventory_data, catalog_data, rule_set, *, event_log):
        return Mapping(
            schema_version=SCHEMA_VERSION,
            inventory_synthetic=False,
            stats=MappingStats(
                source_columns=1,
                mapped_columns=1,
                coverage_percent=100,
                auto=1,
                suggested=0,
                needs_human=0,
            ),
            models=[],
            mappings=[
                FieldMapping(
                    source=SourceRef(table="t", column="c", type="char"),
                    target=TargetRef(model="res.partner", field="name", type="char"),
                    transform=Transform(kind=TransformKind.IDENTITY),
                    confidence=101,  # 引擎缺陷：超出 schema 允許的 0-100
                    tier=Tier.AUTO,
                    reasons=["模擬引擎缺陷"],
                )
            ],
            unmapped=[],
            conflicts=[],
        )

    monkeypatch.setattr(pipeline_module, "build_mapping", _bogus_build_mapping)

    out = tmp_path / "out"
    result = runner.invoke(
        app, ["map", "--inventory", str(clean_inventory_path), "--out", str(out)]
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)
    assert "mapping.schema.json" in result.stdout
    assert "不是你的" in result.stdout
    # 絕對底線：不能誤報成使用者的 --inventory 有問題。
    assert "inventory.schema.json" not in result.stdout
    assert str(clean_inventory_path) not in result.stdout
