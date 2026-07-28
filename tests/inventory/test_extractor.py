"""Inventory 組裝、確定性與 JSON Schema 驗證。"""

import json
import os
from datetime import UTC, datetime

import pytest

from erp_migrai.events import EventLog, Stage
from erp_migrai.inventory.extractor import (
    ScanFailed,
    _read_source_text,
    scan,
    validate_inventory,
    write_inventory,
)
from erp_migrai.inventory.models import (
    SCHEMA_VERSION,
    Column,
    Definition,
    ForeignKey,
    Inventory,
    ProgramUnit,
    SqlStatement,
    Table,
    UnsupportedSyntax,
)

# chmod 000 在 root 下沒有意義（root 會略過檔案權限檢查），這類測試在以 root
# 執行的環境（例如某些容器）下必須乾淨地跳過，而不是用假陽性通過或報錯。
_IS_ROOT = hasattr(os, "getuid") and os.getuid() == 0

FGL = """DATABASE ds

MAIN
    CALL cust_maintain()
END MAIN

FUNCTION cust_maintain()
    DEFINE p_cust RECORD LIKE cust_mast.*
    SELECT COUNT(*) INTO l_cnt FROM cust_mast
    TRY
END FUNCTION
"""

SQL = """CREATE TABLE cust_mast (
    cust_no   CHAR(10)    NOT NULL,
    cust_name VARCHAR(60) NOT NULL,
    PRIMARY KEY (cust_no)
);
"""


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "cust_maintain.4gl").write_text(FGL, encoding="utf-8")
    (d / "schema.sql").write_text(SQL, encoding="utf-8")
    return d


@pytest.fixture
def log(tmp_path):
    return EventLog(
        path=tmp_path / "events.jsonl",
        run_id="run-fixed",
        clock=lambda: datetime(2026, 7, 26, tzinfo=UTC),
        id_factory=lambda: "id-fixed",
    )


def test_scan_collects_tables_programs_and_database(src, log):
    inv = scan(src, event_log=log)
    assert inv.database == "ds"
    assert [t.name for t in inv.tables] == ["cust_mast"]
    assert [(p.kind, p.name) for p in inv.programs] == [
        ("MAIN", None),
        ("FUNCTION", "cust_maintain"),
    ]


def test_source_files_are_relative_and_sorted(src, log):
    inv = scan(src, event_log=log)
    assert inv.source_files == ["cust_maintain.4gl", "schema.sql"]


def test_unsupported_syntax_is_aggregated_at_top_level(src, log):
    inv = scan(src, event_log=log)
    assert any("無法辨識" in u.reason for u in inv.unsupported_syntax)
    assert all(u.file == "cust_maintain.4gl" for u in inv.unsupported_syntax)


def test_each_unsupported_statement_emits_one_e1002(src, log):
    inv = scan(src, event_log=log)
    e1002 = [e for e in log.events if e["error_code"] == "E-1002"]
    assert len(e1002) == len(inv.unsupported_syntax)


def test_unsupported_syntax_does_not_make_scan_fatal(src, log):
    scan(src, event_log=log)
    assert log.has_fatal(Stage.SCAN) is False


def test_synthetic_is_false_without_the_marker(src, log):
    assert scan(src, event_log=log).synthetic is False


def test_synthetic_is_true_with_the_marker(src, log):
    (src / "SYNTHETIC").write_text("合成語料，非真實客戶程式碼\n", encoding="utf-8")
    assert scan(src, event_log=log).synthetic is True


def test_missing_source_directory_raises_and_emits_e1001(tmp_path, log):
    with pytest.raises(ScanFailed):
        scan(tmp_path / "nope", event_log=log)
    assert log.has_fatal(Stage.SCAN) is True


def test_source_path_exists_but_is_not_a_directory_raises_and_emits_e1001(tmp_path, log):
    """`--src` 指到一個存在、但不是目錄的路徑（例如一個一般檔案）時，訊息
    必須跟「路徑根本不存在」區分開來，不能沿用「來源目錄不存在」這個誤導的
    措辭；但仍然是同一個 E-1001、同樣致命，exit code 契約不變。"""
    afile = tmp_path / "afile.txt"
    afile.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ScanFailed, match="存在，但不是目錄"):
        scan(afile, event_log=log)
    assert [e["error_code"] for e in log.events if e["error_code"]] == ["E-1001"]


def test_directory_without_any_4gl_raises_and_emits_e1001(tmp_path, log):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ScanFailed):
        scan(empty, event_log=log)
    assert [e["error_code"] for e in log.events if e["error_code"]] == ["E-1001"]


def test_output_is_deterministic_across_runs(src, tmp_path, log):
    first = write_inventory(scan(src, event_log=log), tmp_path / "a")
    second = write_inventory(scan(src, event_log=log), tmp_path / "b")
    assert first.read_bytes() == second.read_bytes()


def test_output_contains_no_absolute_paths_or_timestamps(src, tmp_path, log):
    path = write_inventory(scan(src, event_log=log), tmp_path / "out")
    text = path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "2026-" not in text


def test_output_is_utf8_unescaped_and_newline_terminated(src, tmp_path, log):
    path = write_inventory(scan(src, event_log=log), tmp_path / "out")
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert "\\u" not in raw


def test_written_inventory_passes_its_own_schema(src, tmp_path, log):
    path = write_inventory(scan(src, event_log=log), tmp_path / "out")
    validate_inventory(json.loads(path.read_text(encoding="utf-8")))


def test_schema_rejects_a_missing_required_field():
    from jsonschema import ValidationError

    with pytest.raises(ValidationError):
        validate_inventory({"synthetic": True})


# --- I6（branch review）：schema mutation testing 存活的四種形狀 ---------------
#
# mutation testing 對 inventory.schema.json 全跑一次測試套件，發現改名
# `$defs.foreign_key.ref_table` / `.columns`、砍掉 `sql_statement.kind` 的
# `DECLARE_CURSOR`、砍掉 `program.kind` 的 `REPORT`、砍掉 `parse_status` 的
# `failed`，這四種 mutation 全部存活——因為 corpus 目前唯一的 case01 剛好
# 沒有外鍵、沒有游標、沒有 REPORT 區塊、也沒有解析失敗的程式單元。這代表
# `foreign_key` 這個 $def 就算被改到拒絕所有真實外鍵，測試套件也不會發現。
#
# 這裡直接從 models.py 的 dataclass（不是任何 corpus 語料、也不透過 scan()）
# 手動組出一份涵蓋每個 $def、每個 enum 值的 Inventory，經 to_dict() round-trip
# 之後驗證——不論 corpus 剛好收錄了什麼語法，這份契約測試都會抓到 schema
# 定義被改壞。


def test_schema_covers_every_def_and_enum_value_the_code_can_produce():
    """涵蓋每個 $def 與每個 enum 值：table（含 primary_key 與 foreign_keys）、
    column（default 為 null 與非 null 各一次）、foreign_key、definition、
    每一種 SqlStatement.kind、每一種 ProgramUnit.kind、每一種 parse_status、
    非空的 database_conflicts、以及頂層與 program 內部的 unsupported_syntax。
    """
    table = Table(
        name="cust_mast",
        columns=[
            Column(name="cust_no", type="CHAR(10)", nullable=False, default=None),
            Column(name="cust_credit", type="DECIMAL(15,2)", nullable=True, default="0"),
        ],
        primary_key=["cust_no"],
        foreign_keys=[
            ForeignKey(columns=["sales_rep"], ref_table="employee", ref_columns=["emp_no"])
        ],
    )

    programs = [
        ProgramUnit(
            kind="GLOBALS",
            name=None,
            file="a.4gl",
            start_line=1,
            end_line=1,
            parse_status="ok",
        ),
        ProgramUnit(
            kind="MAIN",
            name=None,
            file="a.4gl",
            start_line=2,
            end_line=5,
            parse_status="ok",
            definitions=[Definition(name="l_cnt", type="INTEGER", line=3)],
            sql=[SqlStatement(kind="SELECT", tables=["cust_mast"], line=4)],
        ),
        ProgramUnit(
            kind="FUNCTION",
            name="cust_insert",
            file="a.4gl",
            start_line=6,
            end_line=9,
            parse_status="partial",
            sql=[SqlStatement(kind="INSERT", tables=["cust_mast"], line=7)],
            unsupported=[
                UnsupportedSyntax(file="a.4gl", line=8, snippet="TRY", reason="不支援的語句")
            ],
        ),
        ProgramUnit(
            kind="REPORT",
            name="cust_report",
            file="a.4gl",
            start_line=10,
            end_line=14,
            parse_status="failed",
            sql=[
                SqlStatement(kind="UPDATE", tables=["cust_mast"], line=11),
                SqlStatement(kind="DELETE", tables=["cust_mast"], line=12),
                SqlStatement(kind="DECLARE_CURSOR", tables=["cust_mast"], line=13),
            ],
        ),
    ]

    inventory = Inventory(
        synthetic=True,
        schema_version=SCHEMA_VERSION,
        database="ds",
        source_files=["a.4gl", "schema.sql"],
        tables=[table],
        programs=programs,
        unsupported_syntax=[
            UnsupportedSyntax(
                file="schema.sql", line=1, snippet="GARBAGE", reason="非 CREATE TABLE 語句"
            )
        ],
        database_conflicts=["ds_two"],
    )

    validate_inventory(inventory.to_dict())


# --- Resolution 1：BOM 只在 extractor.py 這一處處理 -----------------------------


def test_bom_prefixed_file_parses_identically_to_a_plain_file(tmp_path, log):
    """真實 T100 原始碼常帶 UTF-8 BOM。BOM 曾經（在改用 utf-8-sig 之前）讓檔案
    第一個構造（這裡是 DATABASE 指令）被誤報成無法辨識——因為 BOM 字元讓那一行
    無法匹配任何結構樣式。改用 utf-8-sig 之後，帶 BOM 與不帶 BOM 的同一份原始碼
    必須產生完全相同的結果，且不能因為 BOM 多出一筆不存在的 unsupported 紀錄。
    """
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    (plain_dir / "cust_maintain.4gl").write_text(FGL, encoding="utf-8")
    (plain_dir / "schema.sql").write_text(SQL, encoding="utf-8")
    plain_log = EventLog(
        path=tmp_path / "plain_events.jsonl",
        run_id="run-plain",
        clock=lambda: datetime(2026, 7, 26, tzinfo=UTC),
        id_factory=lambda: "id-plain",
    )
    inv_plain = scan(plain_dir, event_log=plain_log)

    bom_dir = tmp_path / "bom"
    bom_dir.mkdir()
    (bom_dir / "cust_maintain.4gl").write_bytes(b"\xef\xbb\xbf" + FGL.encode("utf-8"))
    (bom_dir / "schema.sql").write_bytes(b"\xef\xbb\xbf" + SQL.encode("utf-8"))
    inv_bom = scan(bom_dir, event_log=log)

    assert inv_bom.database == inv_plain.database == "ds"
    assert [(p.kind, p.name, p.parse_status) for p in inv_bom.programs] == [
        (p.kind, p.name, p.parse_status) for p in inv_plain.programs
    ]
    assert [t.name for t in inv_bom.tables] == [t.name for t in inv_plain.tables]
    # 沒有因為 BOM 而在檔案第一行（DATABASE 指令所在行）多出一筆假的不支援紀錄。
    assert not any(u.line == 1 for u in inv_bom.unsupported_syntax)
    assert len(inv_bom.unsupported_syntax) == len(inv_plain.unsupported_syntax)


# --- Resolution 2：不支援清單彙總不能重複計算 -----------------------------------

GAP_SQL = SQL + "\nCREATE INDEX idx_cust_name ON cust_mast(cust_name);\n"

GAP_FGL = """DATABASE ds

GARBAGE TOP LEVEL LINE

MAIN
    CALL cust_maintain()
END MAIN

FUNCTION cust_maintain()
    DEFINE l_cnt INTEGER
    TRY
END FUNCTION
"""


def test_several_distinct_kinds_of_gap_are_not_duplicated(tmp_path, log):
    """來源同時包含三種不同的落差：schema.sql 裡一句不是 CREATE TABLE 的 DDL、
    .4gl 頂層一行無法辨識的敘述、FUNCTION 內一句無法辨識的語句。三者來自
    extract_tables / SplitResult.unsupported / ProgramUnit.unsupported 三個
    不同來源，彙總後每一筆必須恰好出現一次，且恰好對應一個 E-1002 事件。
    """
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.4gl").write_text(GAP_FGL, encoding="utf-8")
    (d / "schema.sql").write_text(GAP_SQL, encoding="utf-8")

    inv = scan(d, event_log=log)

    keys = [(u.file, u.line) for u in inv.unsupported_syntax]
    assert len(keys) == len(set(keys)), "同一筆落差不應該重複出現"
    assert len(keys) == 3

    e1002 = [e for e in log.events if e["error_code"] == "E-1002"]
    assert len(e1002) == len(inv.unsupported_syntax)


# --- Resolution 3：parse_status 必須原封不動傳到 inventory ----------------------

STATUS_FGL = """DATABASE ds

FUNCTION clean_one()
    DEFINE l_x INTEGER
END FUNCTION

FUNCTION dirty_one()
    DEFINE l_y INTEGER
    TRY
END FUNCTION
"""


def test_parse_status_survives_into_inventory_and_differs_across_programs(tmp_path, log):
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.4gl").write_text(STATUS_FGL, encoding="utf-8")

    inv = scan(d, event_log=log)

    statuses = {p.name: p.parse_status for p in inv.programs}
    assert statuses["clean_one"] == "ok"
    assert statuses["dirty_one"] == "partial"
    assert statuses["clean_one"] != statuses["dirty_one"]


# --- 邊界情況：空白／只有註解的 .4gl ---------------------------------------------


def test_comment_only_4gl_produces_no_programs_without_crashing(tmp_path, log):
    d = tmp_path / "src"
    d.mkdir()
    (d / "empty.4gl").write_text("-- 這個檔案只有註解，沒有任何程式碼\n", encoding="utf-8")

    inv = scan(d, event_log=log)

    assert inv.programs == []
    assert inv.unsupported_syntax == []
    assert inv.source_files == ["empty.4gl"]


# --- 邊界情況：只有 schema.sql、沒有任何 .4gl 仍是 E-1001 -----------------------


def test_directory_with_only_schema_sql_still_raises_e1001(tmp_path, log):
    d = tmp_path / "src"
    d.mkdir()
    (d / "schema.sql").write_text(SQL, encoding="utf-8")

    with pytest.raises(ScanFailed):
        scan(d, event_log=log)
    assert log.has_fatal(Stage.SCAN) is True


# --- 邊界情況：兩個檔案宣告不同的 DATABASE ---------------------------------------

CONFLICT_A = """DATABASE ds_one

MAIN
    CALL noop()
END MAIN
"""

CONFLICT_B = """DATABASE ds_two

FUNCTION noop()
END FUNCTION
"""


def test_conflicting_database_directives_pick_first_by_sorted_filename_and_are_reported(
    tmp_path, log
):
    """inventory.json 的 database 欄位（Task 6 不能修改 models.py）只能表達單一
    名稱，兩個檔案宣告不同名稱時勢必有一個「輸」。這裡驗證兩件事：勝出的名稱
    是依排序後檔名的第一個檔案（確定性、可預期），而輸掉的宣告不是悄悄消失——
    events.jsonl 必須留下一筆可稽核的衝突紀錄。
    """
    d = tmp_path / "src"
    d.mkdir()
    (d / "a_first.4gl").write_text(CONFLICT_A, encoding="utf-8")
    (d / "b_second.4gl").write_text(CONFLICT_B, encoding="utf-8")

    inv = scan(d, event_log=log)

    assert inv.database == "ds_one"
    conflict_events = [e for e in log.events if e["event_type"] == "INVENTORY_DATABASE_CONFLICT"]
    assert len(conflict_events) == 1
    assert "ds_one" in conflict_events[0]["message"]
    assert "ds_two" in conflict_events[0]["message"]
    # 衝突不是致命錯誤，不使用任何錯誤碼，也不影響 exit code。
    assert conflict_events[0]["error_code"] is None
    assert log.has_fatal(Stage.SCAN) is False


# --- 邊界情況：無法解碼的原始碼（例如 Big5 的舊系統遺留檔案）--------------------


def test_undecodable_source_file_is_reported_not_crashed(tmp_path, log):
    """UnicodeDecodeError 逃出 scan 會違反「絕不中止」的不變量。這裡驗證：
    一份 Big5 編碼、不是合法 UTF-8（含 BOM）位元組序列的 .4gl 檔案不會讓 scan
    拋出未預期的例外，而是被記錄成一筆 UnsupportedSyntax；且不會因為這一個壞檔
    就放棄目錄裡其他可以正常解碼的檔案。
    """
    d = tmp_path / "src"
    d.mkdir()
    (d / "good.4gl").write_text(FGL, encoding="utf-8")
    (d / "legacy.4gl").write_bytes("客戶資料維護 CHAR(10)".encode("big5"))

    inv = scan(d, event_log=log)

    assert "legacy.4gl" in inv.source_files
    assert any(u.file == "legacy.4gl" and "解碼" in u.reason for u in inv.unsupported_syntax)
    # 另一個可解碼的檔案仍然正常處理。
    assert any(p.name == "cust_maintain" for p in inv.programs)
    # 解碼失敗一樣要有對應的 E-1002 事件，且不影響 exit code。
    e1002 = [e for e in log.events if e["error_code"] == "E-1002"]
    assert len(e1002) == len(inv.unsupported_syntax)
    assert log.has_fatal(Stage.SCAN) is False


# --- Finding 1：非解碼類的 I/O 錯誤（OSError）不能逃出 scan --------------------


@pytest.mark.skipif(_IS_ROOT, reason="root 會略過檔案權限檢查，chmod 000 測試在 root 下沒有意義")
def test_unreadable_source_file_is_reported_not_crashed(tmp_path, log):
    """`_read_source_text` 舊版只接住 `UnicodeDecodeError`；檔案存在、is_file()
    通過，但實際讀取時因為權限被拿掉而拋出 `PermissionError`（`OSError` 的子類別）
    ，這種例外會直接逃出 `scan()`，違反「scan 絕不中止」的不變量。這裡驗證：
    chmod 000 的檔案不會讓 scan 崩潰，而是被記錄成一筆 UnsupportedSyntax，且
    不影響目錄裡其他檔案的掃描。
    """
    d = tmp_path / "src"
    d.mkdir()
    (d / "good.4gl").write_text(FGL, encoding="utf-8")
    locked = d / "locked.4gl"
    locked.write_text(FGL, encoding="utf-8")
    locked.chmod(0o000)
    try:
        inv = scan(d, event_log=log)
    finally:
        # 讓 tmp_path 之後可以正常被 pytest 清理掉。
        locked.chmod(0o644)

    assert "locked.4gl" in inv.source_files
    matches = [u for u in inv.unsupported_syntax if u.file == "locked.4gl"]
    assert len(matches) == 1
    # 理由必須點名是 I/O 問題（而非誤判成解碼失敗），且要能認出實際的例外型別。
    assert "I/O" in matches[0].reason
    assert "PermissionError" in matches[0].reason
    # 另一個可讀取的檔案仍然正常處理，不因為一個壞檔就放棄整個目錄。
    assert any(p.name == "cust_maintain" for p in inv.programs)
    e1002 = [e for e in log.events if e["error_code"] == "E-1002"]
    assert len(e1002) == len(inv.unsupported_syntax)
    assert log.has_fatal(Stage.SCAN) is False


def test_read_source_text_reports_io_error_for_a_directory(tmp_path):
    """`scan()` 本身會先用 `is_file()` 過濾掉目錄，這裡直接測試 `_read_source_text`
    這個輔助函式本身：萬一有其他呼叫端（或未來 `is_file()` 與實際讀取之間出現
    TOCTOU 競態）把一個目錄路徑傳進來，`open()` 一個目錄會拋出
    `IsADirectoryError`（`OSError` 的子類別），這裡必須被接住並轉成
    `UnsupportedSyntax`，而不是讓例外逃出去。
    """
    d = tmp_path / "looks_like_a_file.4gl"
    d.mkdir()

    text, entry = _read_source_text(d)

    assert text is None
    assert entry is not None
    assert entry.file == d.name
    assert "I/O" in entry.reason
    assert "IsADirectoryError" in entry.reason


# --- Finding 2：DATABASE 衝突必須連同 database_conflicts 一起進 inventory.json --

THREE_WAY_A = """DATABASE ds_a

MAIN
    CALL noop()
END MAIN
"""

THREE_WAY_B = """DATABASE ds_b

FUNCTION noop()
END FUNCTION
"""

THREE_WAY_C = """DATABASE ds_c

FUNCTION another()
END FUNCTION
"""

SAME_NAME_A = """DATABASE ds_same

MAIN
    CALL noop()
END MAIN
"""

SAME_NAME_B = """DATABASE ds_same

FUNCTION noop()
END FUNCTION
"""


def test_database_conflict_is_recorded_in_inventory_json(tmp_path, log):
    """先前 database 衝突只寫進 events.jsonl，inventory.json（公開格式，商業版
    consumer 只讀這份檔案）裡完全看不出還有第二個宣告存在。這裡驗證衝突的
    「事實本身」現在會進到 inventory.json 的 database_conflicts 欄位，且寫出的
    JSON 仍然通過 schema 驗證。
    """
    d = tmp_path / "src"
    d.mkdir()
    (d / "a_first.4gl").write_text(CONFLICT_A, encoding="utf-8")
    (d / "b_second.4gl").write_text(CONFLICT_B, encoding="utf-8")

    inv = scan(d, event_log=log)

    assert inv.database == "ds_one"
    assert inv.database_conflicts == ["ds_two"]

    path = write_inventory(inv, tmp_path / "out")
    validate_inventory(json.loads(path.read_text(encoding="utf-8")))


def test_database_conflicts_is_empty_in_the_normal_case(src, log):
    inv = scan(src, event_log=log)
    assert inv.database_conflicts == []


def test_three_files_three_different_database_names_records_both_others_sorted(tmp_path, log):
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.4gl").write_text(THREE_WAY_A, encoding="utf-8")
    (d / "b.4gl").write_text(THREE_WAY_B, encoding="utf-8")
    (d / "c.4gl").write_text(THREE_WAY_C, encoding="utf-8")

    inv = scan(d, event_log=log)

    assert inv.database == "ds_a"
    assert inv.database_conflicts == ["ds_b", "ds_c"]


def test_two_files_declaring_the_same_database_name_records_no_conflict(tmp_path, log):
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.4gl").write_text(SAME_NAME_A, encoding="utf-8")
    (d / "b.4gl").write_text(SAME_NAME_B, encoding="utf-8")

    inv = scan(d, event_log=log)

    assert inv.database == "ds_same"
    assert inv.database_conflicts == []
    conflict_events = [e for e in log.events if e["event_type"] == "INVENTORY_DATABASE_CONFLICT"]
    assert conflict_events == []


# --- Finding 3：unsupported_syntax 的排序鍵必須是完整的 total order ------------

# 重現一份會讓 (file, line) 平手的 schema.sql：同一個未終止的單引號字串，會先
# 被 split_statements 記一筆「單引號字串未終止」，接著整段殘餘文字被當成一句
# 語句交給 extract_tables，因為不是 CREATE TABLE 又補記一筆「非 CREATE TABLE
# 語句」——兩筆的 file、line 完全相同，只有 reason／snippet 不同。
TIE_SQL = """CREATE TABLE cust_mast (
    cust_no CHAR(10)
);

'oops
some garbage;
"""


def test_unsupported_syntax_tie_break_uses_reason_and_snippet_for_a_total_order(tmp_path, log):
    """`(file, line)` 不是 total order：這份 schema.sql 在同一行產生兩筆不同
    肇因的落差。排序鍵補上 reason、snippet 之後，兩者的相對順序必須是確定的
    （不再依賴 Python 穩定排序 + 產生順序這個沒有明說的隱含假設），這裡把順序
    斷言出來，之後任何 producer 調整內部順序都會被這個測試抓到。
    """
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.4gl").write_text(FGL, encoding="utf-8")
    (d / "schema.sql").write_text(TIE_SQL, encoding="utf-8")

    inv = scan(d, event_log=log)

    tied = [u for u in inv.unsupported_syntax if u.file == "schema.sql" and u.line == 5]
    assert len(tied) == 2
    assert tied[0].reason.startswith("單引號字串未終止")
    assert tied[1].reason.startswith("非 CREATE TABLE 語句")


def test_two_scans_of_the_tied_unsupported_input_are_byte_identical(tmp_path, log):
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.4gl").write_text(FGL, encoding="utf-8")
    (d / "schema.sql").write_text(TIE_SQL, encoding="utf-8")

    first = write_inventory(scan(d, event_log=log), tmp_path / "out_a")
    second = write_inventory(scan(d, event_log=log), tmp_path / "out_b")

    assert first.read_bytes() == second.read_bytes()


# --- C1（branch review）：副檔名大小寫必須不分大小寫地被發現 --------------------
#
# 舊版用 `src_dir.glob("*.4gl")` 與 `src_dir / "schema.sql"` 這兩個大小寫敏感的
# 判準找檔案。真實 T100 原始碼常來自 Windows 工作站，大寫副檔名（`.4GL`）司空
# 見慣；`glob` 在所有平台上都是大小寫敏感的，但 `Path.is_file()` 委派給
# `os.stat`，在大小寫不敏感的檔案系統（macOS APFS 預設）上會找到
# `SCHEMA.SQL`，在 Linux 上則找不到——同一份輸入在兩個平台上產生不同的
# inventory.json，golden 檔案因此變得依賴平台。修正後改成掃描目錄一次，
# 用 `.lower()` 判斷副檔名／檔名，兩個平台的行為就會一致。

MIXED_CASE_MAIN = """DATABASE ds

MAIN
    CALL noop()
END MAIN
"""

MIXED_CASE_FUNC = """FUNCTION noop()
    DEFINE l_x INTEGER
END FUNCTION
"""


def test_mixed_case_extension_and_schema_filename_are_all_discovered(tmp_path, log):
    """`a.4gl`、大寫副檔名的 `CUST_B.4GL`、大寫檔名的 `SCHEMA.SQL` 三個檔案都要
    被發現、被消化：三者全部出現在 source_files，SCHEMA.SQL 的表被擷取，兩個
    .4gl 檔的程式單元都被解析，且不產生任何不支援清單。"""
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.4gl").write_text(MIXED_CASE_MAIN, encoding="utf-8")
    (d / "CUST_B.4GL").write_text(MIXED_CASE_FUNC, encoding="utf-8")
    (d / "SCHEMA.SQL").write_text(SQL, encoding="utf-8")

    inv = scan(d, event_log=log)

    assert inv.source_files == ["CUST_B.4GL", "SCHEMA.SQL", "a.4gl"]
    assert [t.name for t in inv.tables] == ["cust_mast"]
    # 依實際檔名排序：CUST_B.4GL 在 a.4gl 之前（大寫字母的 code point 較小）。
    assert [(p.kind, p.name) for p in inv.programs] == [
        ("FUNCTION", "noop"),
        ("MAIN", None),
    ]
    assert inv.unsupported_syntax == []


def test_unrecognized_file_produces_one_unsupported_entry_naming_it(src, log):
    """目錄裡一個既不是 .4gl 也不是 schema.sql 的雜散檔案，不能悄悄消失：必須
    產生恰好一筆 UnsupportedSyntax、點名檔名，但不影響 exit code（不是致命
    錯誤，仍走 E-1002）。"""
    (src / "notes.txt").write_text("這不是本階段認得的原始碼\n", encoding="utf-8")

    inv = scan(src, event_log=log)

    stray = [u for u in inv.unsupported_syntax if u.file == "notes.txt"]
    assert len(stray) == 1
    assert stray[0].line == 1
    assert "notes.txt" in stray[0].snippet
    assert ".4gl" in stray[0].reason
    assert "schema.sql" in stray[0].reason
    assert "notes.txt" not in inv.source_files
    assert log.has_fatal(Stage.SCAN) is False


def test_two_schema_files_differing_only_by_case_pick_first_and_report_the_other(tmp_path, log):
    """在大小寫敏感的檔案系統上，一個目錄可以同時有 `schema.sql` 與
    `SCHEMA.SQL`。排序後在前的那個當作 schema 使用，另一個必須透過「未消化」
    機制被回報，而不是悄悄消失。"""
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.4gl").write_text(FGL, encoding="utf-8")
    (d / "SCHEMA.SQL").write_text(SQL, encoding="utf-8")
    (d / "schema.sql").write_text(SQL, encoding="utf-8")

    inv = scan(d, event_log=log)

    assert [t.name for t in inv.tables] == ["cust_mast"]
    # "SCHEMA.SQL" 排序在 "schema.sql" 之前（大寫字母 code point 較小），
    # 因此前者被當成 schema 使用、出現在 source_files；後者被回報為未消化。
    assert "SCHEMA.SQL" in inv.source_files
    assert "schema.sql" not in inv.source_files
    unconsumed = [u for u in inv.unsupported_syntax if u.file == "schema.sql"]
    assert len(unconsumed) == 1
    assert unconsumed[0].line == 1


def test_synthetic_marker_is_never_reported_as_unconsumed(src, log):
    (src / "SYNTHETIC").write_text("合成語料，非真實客戶程式碼\n", encoding="utf-8")

    inv = scan(src, event_log=log)

    assert not any(u.file == "SYNTHETIC" for u in inv.unsupported_syntax)


def test_subdirectory_is_not_recursed_into_or_reported_as_unconsumed(src, log):
    """C1 修正保持既有的扁平（不遞迴）行為：子目錄不是一般檔案，不該被當成
    「未消化」回報。"""
    sub = src / "sub"
    sub.mkdir()
    (sub / "hidden.4gl").write_text(FGL, encoding="utf-8")

    inv = scan(src, event_log=log)

    assert not any(u.file == "sub" for u in inv.unsupported_syntax)
    assert "sub" not in inv.source_files
