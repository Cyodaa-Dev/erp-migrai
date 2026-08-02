"""golden-file 測試：每個 corpus case 的 scan 產出必須與 expected/ 逐位元組相符。

除了 golden 比對，case01 另有具名的性質斷言——golden 檔是回歸保護，
性質斷言才是真正說明「我們期待什麼行為」的地方。
"""

import json

import pytest
from conftest import CORPUS_ROOT, corpus_cases

from erp_migrai.events import EventLog, Stage
from erp_migrai.inventory.extractor import scan, validate_inventory, write_inventory


def _scan_case(case, tmp_path):
    log = EventLog(path=tmp_path / "events.jsonl", run_id="test")
    inventory = scan(case / "src", event_log=log)
    return inventory, log


def test_corpus_discovery_finds_at_least_one_case():
    """發現機制不能悄悄找到零個 case。

    這個測試刻意不吃 `corpus_case` fixture：若 `conftest.corpus_cases()`
    因為根目錄打錯字、改名、或某個 case 少了 expected/ 而回傳空清單，
    `pytest_generate_tests` 只會把參數化測試變成零個測試項目——那些測試
    會「消失」而不是「失敗」，整體結果看起來像全數通過。此測試獨立於
    參數化集合之外執行，才能在「一個都沒找到」時真正紅燈。
    """
    cases = corpus_cases()
    assert len(cases) >= 1, (
        f"{CORPUS_ROOT} 底下沒有發現任何具備 src/ 與 expected/ 的 case，語料庫自動發現可能已經失效"
    )


def test_every_corpus_directory_is_a_complete_case():
    """半成品 case 不能被 `corpus_cases()` 的過濾條件悄悄吸收成「沒被發現」。

    `corpus_cases()` 只挑出同時具備 `src/` 與 `expected/` 的目錄，這對
    `pytest_generate_tests` 的參數化很合理，但也表示一個貢獻者若新增了
    `corpus/caseNN_xxx/src/`、忘記跑 `make update-golden` 補上
    `expected/inventory.json`（或 `expected/mapping.json`），這個半成品
    目錄會被直接濾掉、完全不參與任何測試——建置結果看起來是全綠的，卻少了
    一整組本該存在的測試。

    這裡刻意**不呼叫** `corpus_cases()`，而是直接、未經過濾地走訪
    `corpus/` 底下每一個目錄，逐一檢查它是否同時具備 `src/`、
    `expected/inventory.json`、`expected/mapping.json`。任何一個目錄少了
    其中之一，都要讓整個測試套件在這裡刺眼地失敗，並在錯誤訊息裡點名是哪個
    case、缺了什麼。

    加入 `mapping.json` 之後（map 階段），這條守則要一併延伸：一個 case
    若已經有 `expected/inventory.json` 但還沒有 `expected/mapping.json`，
    這就是 map 階段的半成品，不能被悄悄放過——延續 scan 階段就已經建立的
    「半成品必須指名道姓地紅燈」守則，而不是另外開一條更嚴格的
    `corpus_cases()` 過濾條件把這種 case 從參數化測試裡整個濾掉（那只會讓
    它「消失」而不是「失敗」）。
    """
    if not CORPUS_ROOT.is_dir():
        return  # 根目錄本身不存在由上一個測試負責報告，這裡沒有目錄可檢查
    for entry in sorted(CORPUS_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        missing = []
        if not (entry / "src").is_dir():
            missing.append("src/")
        if not (entry / "expected" / "inventory.json").is_file():
            missing.append("expected/inventory.json")
        if not (entry / "expected" / "mapping.json").is_file():
            missing.append("expected/mapping.json")
        assert not missing, (
            f"{entry.name} 缺少 {'、'.join(missing)}——這是半成品 case，"
            "請執行 `make update-golden` 產生並審閱 golden 之後再送出"
        )


def test_golden_inventory_matches(corpus_case, tmp_path):
    inventory, _ = _scan_case(corpus_case, tmp_path)
    produced = write_inventory(inventory, tmp_path / "out")
    golden = corpus_case / "expected" / "inventory.json"
    assert golden.is_file(), f"{corpus_case.name} 缺少 expected/inventory.json"
    assert produced.read_text(encoding="utf-8") == golden.read_text(encoding="utf-8")


def test_golden_inventory_passes_schema(corpus_case):
    golden = corpus_case / "expected" / "inventory.json"
    validate_inventory(json.loads(golden.read_text(encoding="utf-8")))


def test_every_case_is_marked_synthetic(corpus_case, tmp_path):
    """語料庫全數標 synthetic: true——這是誠實性設計，不是選配。"""
    inventory, _ = _scan_case(corpus_case, tmp_path)
    assert inventory.synthetic is True


def test_scan_of_any_case_is_never_fatal(corpus_case, tmp_path):
    _, log = _scan_case(corpus_case, tmp_path)
    assert log.has_fatal(Stage.SCAN) is False


class TestCase01Partner:
    """case01 的具名性質斷言。"""

    @pytest.fixture
    def inventory(self, corpus_root, tmp_path):
        log = EventLog(path=tmp_path / "events.jsonl", run_id="test")
        return scan(corpus_root / "case01_partner" / "src", event_log=log)

    def test_database_is_ds(self, inventory):
        assert inventory.database == "ds"

    def test_single_table_with_seven_columns(self, inventory):
        assert [t.name for t in inventory.tables] == ["cust_mast"]
        assert len(inventory.tables[0].columns) == 7

    def test_primary_key_is_cust_no(self, inventory):
        assert inventory.tables[0].primary_key == ["cust_no"]

    def test_decimal_precision_is_preserved(self, inventory):
        types = {c.name: c.type for c in inventory.tables[0].columns}
        assert types["cust_credit"] == "DECIMAL(15,2)"

    def test_program_units_found_in_source_order(self, inventory):
        assert [(p.kind, p.name) for p in inventory.programs] == [
            ("GLOBALS", None),
            ("MAIN", None),
            ("FUNCTION", "cust_maintain"),
            ("FUNCTION", "cust_insert"),
            ("FUNCTION", "cust_update"),
            ("FUNCTION", "cust_delete"),
        ]

    def test_case01_has_no_unsupported_syntax(self, inventory):
        """case01 是乾淨的 happy path，不含任何不支援語法；混入不支援語法的
        情境留給未來新增的 case 覆蓋。"""
        assert inventory.unsupported_syntax == []
        assert {p.parse_status for p in inventory.programs} == {"ok"}

    def test_all_four_sql_kinds_are_detected(self, inventory):
        kinds = {s.kind for p in inventory.programs for s in p.sql}
        assert kinds == {"SELECT", "INSERT", "UPDATE", "DELETE"}

    def test_record_like_type_is_captured(self, inventory):
        types = {d.type for p in inventory.programs for d in p.definitions}
        assert "RECORD LIKE cust_mast.*" in types
        assert "LIKE cust_mast.cust_no" in types

    def test_database_conflicts_is_empty(self, inventory):
        """單一 DATABASE 宣告的專案不應出現任何衝突名稱（Resolution 2）。"""
        assert inventory.database_conflicts == []


def test_case03_references_are_all_rule_declared():
    """case03 的每一條 reference 都必須來自規則檔宣告。

    🔴 2026-08-02 修訂：本測試原本斷言「兩條產生 reference 的路徑都活著」，
    但 Task 3 實測證明「從 DDL 外鍵自動產生 reference」是永遠打不開的死碼，
    昴君已裁決整段拿掉。現在 reference 只有一個來源：規則檔明確宣告
    （= 貢獻點③，也是設計 spec P3-5 本來就寫明的主路徑）。

    仍然保留對 ord_cust_no 與 ord_prod_no 兩筆的斷言，因為它們證明的是
    不同的事：ord_cust_no 的 DDL **有**宣告 FK（規則與 DDL 一致，
    不該發衝突）；ord_prod_no 的 DDL **刻意沒有**宣告（真實 T100 最常見的
    形狀，只能靠規則補）。兩者都要走得通。

    🔴 Task 4.5 修訂：map 階段新增「同一個目標欄位被一個以上的來源欄位對應」
    的檢查（`ConflictKind.TARGET`）後，第一時間在 `sale_order.yaml` 抓到一個
    真缺陷——`ord_mast.ord_no`（規則，confidence 100）與 `ord_mast.
    ord_cust_ref`（啟發式，confidence 30）都對到 `sale.order.
    client_order_ref`，且配錯了語意：`ord_cust_ref` 才是「客戶自己的訂單
    編號」，即 catalog 對 `client_order_ref` 的定義；`ord_no` 是 T100 自己
    的訂單編號，語意上該對到 `sale.order.name`。這一輪修訂把
    `sale_order.yaml` 改正（`ord_cust_ref → client_order_ref`、
    `ord_no → name`），衝突的根源消失，`conflicts` 因此變回 `[]`——
    這是檢查機制運作正常的結果（抓到真缺陷、缺陷被修掉），不是檢查被弱化。
    檢查機制本身仍然活著，由 `tests/mapping/test_engine.py` 的
    `test_two_source_columns_ruled_to_same_target_field_produce_target_conflict`
    與 `test_target_conflict_spans_auto_and_needs_human_tiers_like_case03`
    守著（兩條都用自訂 `RuleSet` 構造衝突情境，不依賴這份語料）。
    """
    mapping = json.loads(
        (CORPUS_ROOT / "case03_sales_order" / "expected" / "mapping.json").read_text(
            encoding="utf-8"
        )
    )
    by_source = {(m["source"]["table"], m["source"]["column"]): m for m in mapping["mappings"]}

    cust_ref = by_source[("ord_mast", "ord_cust_no")]["transform"]
    assert cust_ref["kind"] == "reference"
    assert cust_ref["ref_table"] == "cust_mast"
    assert cust_ref["ref_column"] == "cust_no"

    prod_ref = by_source[("ord_deta", "ord_prod_no")]["transform"]
    assert prod_ref["kind"] == "reference"
    assert prod_ref["ref_table"] == "prod_mast"
    assert prod_ref["ref_column"] == "prod_no"

    assert mapping["conflicts"] == []

    ord_no_mapping = by_source[("ord_mast", "ord_no")]
    assert ord_no_mapping["target"]["field"] == "name"
    assert ord_no_mapping["tier"] == "auto"

    ord_cust_ref_mapping = by_source[("ord_mast", "ord_cust_ref")]
    assert ord_cust_ref_mapping["target"]["field"] == "client_order_ref"
    assert ord_cust_ref_mapping["tier"] == "auto"
    assert ord_cust_ref_mapping["confidence"] == 100


def test_case03_reports_unsupported_syntax_but_data_tables_are_complete():
    """不支援語法影響程式盤點，不影響資料遷移——這兩件事必須同時成立。

    這是總設計 §3.2 的核心宣稱，也是計劃 3 要證明的行為。若這條測試因為
    unsupported_syntax 變空而失敗，代表混入的語法其實被支援了，case03 就
    失去它存在的一半理由。
    """
    inventory = json.loads(
        (CORPUS_ROOT / "case03_sales_order" / "expected" / "inventory.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(inventory["unsupported_syntax"]) >= 2
    table_names = {t["name"] for t in inventory["tables"]}
    assert table_names == {"cust_mast", "prod_mast", "ord_mast", "ord_deta"}
