"""`verify.py` 比對邏輯的測試：純函式，本機 100% 測得到。

🔴 控制者已代跑的 PRE-FLIGHT 指出一個關鍵陷阱：`compare_amount` 的容差是
`Decimal("0.01")`，而 `Decimal(v)`／`Decimal(str(v))` 兩種實作在 case03 的
真實資料上算出的差額只有 `4.77e-14`——遠小於容差，**兩種實作的 `status`
都是 `pass`**。任何只斷言 `c.status == "pass"` 的測試分辨不出正確與錯誤的
實作。這裡對關鍵測試改斷言「報告裡的 `source_amount` 字串」（例如
`"26999.99"`），這條分辨得出來：`Decimal(v)` 會產出
`"26999.98999999999995225152816"` 這種帶雜訊的字串，`Decimal(str(v))`
才會產出乾淨的 `"26999.99"`。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from erp_migrai.events import E_5001, E_5002
from erp_migrai.pipeline.verify import (
    KINDS,
    STATUSES,
    TARGET_LINE_AMOUNT_FIELD,
    VERIFICATION_ROUNDING,
    Check,
    MigrationLogReadError,
    assemble_checks,
    compare_amount,
    compare_row_count,
    count_names_with_prefix,
    fatal_code_for,
    find_amount_subtotal_target,
    find_mismatched_external_ids,
    find_row_count_targets,
    ir_model_data_domain,
    parse_migration_log,
    res_ids_with_prefix,
    resolve_synthetic,
    resolve_target_counts,
    sum_source_amount,
    sum_target_amount,
)

# --------------------------------------------------------------------------
# Check：kind／status 兩個 enum，各恰好兩個值
# --------------------------------------------------------------------------


def test_kinds_are_exactly_two_values():
    assert {"row_count", "amount_subtotal"} == KINDS


def test_statuses_are_exactly_two_values():
    assert {"pass", "fail"} == STATUSES


# --------------------------------------------------------------------------
# compare_row_count：完全相等才 pass，筆數沒有容差
# --------------------------------------------------------------------------


def test_row_count_match_is_pass():
    c = compare_row_count("cust_mast", "res.partner", 3, 3)
    assert c.status == "pass"
    assert c.kind == "row_count"
    assert c.source_count == 3
    assert c.target_count == 3
    assert c.source_amount is None
    assert c.target_amount is None


def test_row_count_mismatch_is_fail():
    """夾具刻意非對稱（7 vs 3，不是隨便挑的兩個相近數字）：只斷言
    `status` 分辨不出『兩個欄位對調』（審查修正輪次 1 Critical C3），這裡
    額外斷言 `source_count`／`target_count` 真的各自對到哪個引數。
    """
    c = compare_row_count("cust_mast", "res.partner", 7, 3)
    assert c.status == "fail"
    assert c.source_count == 7
    assert c.target_count == 3


def test_row_count_has_no_tolerance():
    """筆數差 1 就是 fail——不像金額有 0.01 的容差。"""
    c = compare_row_count("cust_mast", "res.partner", 100, 99)
    assert c.status == "fail"


# --------------------------------------------------------------------------
# compare_amount：0.01 容差內視為 pass
# --------------------------------------------------------------------------


def test_amount_within_currency_rounding_is_pass():
    """0.01 以內視為相等——浮點數與 Decimal 的表示差異不該亮紅燈。"""
    c = compare_amount(
        "ord_deta",
        "sale.order.line",
        Decimal("12345.00"),
        Decimal("12345.00"),
        rounding=Decimal("0.01"),
    )
    assert c.status == "pass"
    assert c.kind == "amount_subtotal"
    assert c.source_count is None
    assert c.target_count is None


def test_amount_beyond_rounding_is_fail():
    """夾具刻意非對稱：只斷言 `status` 分辨不出『兩個金額對調』（審查修正
    輪次 1 Critical C3），這裡額外斷言 `source_amount`／`target_amount`
    的字串各自對到哪個引數。
    """
    c = compare_amount(
        "ord_deta",
        "sale.order.line",
        Decimal("100.00"),
        Decimal("90.00"),
        rounding=Decimal("0.01"),
    )
    assert c.status == "fail"
    assert c.source_amount == "100.00"
    assert c.target_amount == "90.00"


def test_amount_exactly_at_rounding_boundary_is_pass():
    """容差是 `abs(diff) <= rounding`，不是 `<`——差恰好 0.01 仍算 pass。"""
    c = compare_amount(
        "ord_deta",
        "sale.order.line",
        Decimal("100.00"),
        Decimal("100.01"),
        rounding=Decimal("0.01"),
    )
    assert c.status == "pass"


def test_amount_just_beyond_rounding_boundary_is_fail():
    c = compare_amount(
        "ord_deta",
        "sale.order.line",
        Decimal("100.00"),
        Decimal("100.02"),
        rounding=Decimal("0.01"),
    )
    assert c.status == "fail"


def test_compare_amount_stores_amounts_as_strings():
    """`source_amount`／`target_amount` 存的是字串，不是 Decimal 或 float
    ——verification_report.schema.json 的型別是 `["string", "null"]`。"""
    c = compare_amount(
        "ord_deta",
        "sale.order.line",
        Decimal("12345.00"),
        Decimal("12345.00"),
        rounding=Decimal("0.01"),
    )
    assert isinstance(c.source_amount, str)
    assert isinstance(c.target_amount, str)


# --------------------------------------------------------------------------
# sum_source_amount：float 地雷的真正防線
# --------------------------------------------------------------------------


def test_sum_source_amount_from_sqlite_floats_do_not_drift():
    """SQLite 的 DECIMAL 讀回來是 float；轉 Decimal 時不可以帶進二進位雜訊。

    已實跑確認（uv run python）：
        rows = [0.1] * 10
        sum(Decimal(v)      for v in rows) == Decimal("1.000000000000000055511151231")
        sum(Decimal(str(v)) for v in rows) == Decimal("1.0")

    注意：float 的 `sum([0.1]*10)` 在 Python 裡剛好就是 `1.0`，所以「用
    float 累加」不是這裡的破口——破口是 `Decimal(float)` 這個建構式本身。
    這條測試同時做兩件事：(1) 直接比較 `Decimal(v)` 與 `Decimal(str(v))`
    確認兩者真的不同（守住測試本身沒有失效）；(2) 呼叫真正的
    `sum_source_amount` 確認它產出的是「乾淨」那一邊。
    """
    rows = [{"qty": 1, "price": 0.1} for _ in range(10)]  # 模擬 SqliteSourceReader 吐出的 float

    naive = sum((Decimal(row["price"]) for row in rows), Decimal("0"))
    assert naive != Decimal("1.0"), "這條測試本身失效了——Decimal(float) 竟然沒有誤差"

    total = sum_source_amount(rows, qty_column="qty", price_column="price")
    assert total == Decimal("1.0")


def test_sum_source_amount_on_real_case03_data_matches_hand_calculated_total():
    """用真的 `case03_sales_order` 語料（`make corpus` 產生的 `data.sqlite`）
    算一次 `ord_deta` 的合計，跟 README／seed.sql 手算的 `26999.99` 核對。

    🔴 控制者已代跑的 PRE-FLIGHT：`Decimal(v)`（誤用 float 直接建構）在這份
    真實資料上算出 `26999.98999999999995225152816`，跟正確答案的差額只有
    `4.77e-14`——遠小於 `compare_amount` 的 `0.01` 容差，兩種實作的
    `status` 都會是 `pass`。這裡直接斷言 `sum_source_amount` 的回傳值
    **恰好等於** `Decimal("26999.99")`（而不是只斷言某個 `compare_amount`
    呼叫回傳 `pass`），才分辨得出兩種實作。
    """
    pytest.importorskip("erp_migrai.tools.build_corpus")
    from erp_migrai.tools.build_corpus import build_case

    corpus_root = Path(__file__).resolve().parents[2] / "corpus"
    case_dir = _copy_case03_sources(corpus_root)
    build_case(case_dir)

    con = sqlite3.connect(case_dir / "data" / "data.sqlite")
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in con.execute("SELECT * FROM ord_deta ORDER BY ord_no, ord_seq")]
    finally:
        con.close()

    assert len(rows) == 5, "ord_deta 應該有 5 列明細（見任務書『語料的真實數字』）"

    # 對照組：若誤用 Decimal(value) 而非 Decimal(str(value))，這份真實資料
    # 上會產出帶雜訊的合計，跟手算答案不相等。
    naive_total = sum(
        (Decimal(row["ord_qty"]) * Decimal(row["ord_price"]) for row in rows), Decimal("0")
    )
    assert naive_total != Decimal("26999.99"), "naive 實作在真實資料上應該要有雜訊"

    total = sum_source_amount(rows, qty_column="ord_qty", price_column="ord_price")
    assert total == Decimal("26999.99")
    assert str(total) == "26999.99"

    # 用 compare_amount 包一層，斷言的是字串，而不是只看 status（見模組
    # docstring：4.77e-14 遠小於 0.01 容差，兩種實作的 status 都會是 pass）。
    check = compare_amount(
        "ord_deta", "sale.order.line", total, Decimal("26999.99"), rounding=Decimal("0.01")
    )
    assert check.status == "pass"
    assert check.source_amount == "26999.99"


def _copy_case03_sources(corpus_root: Path, tmp_root: Path | None = None) -> Path:
    """把 case03 的 `src/schema.sql`／`data/seed.sql` 複製到一個乾淨的臨時
    目錄，讓 `build_case` 現場產生 `data.sqlite`——不依賴倉庫裡任何先前
    執行留下的檔案（precedent：`tests/test_cli_load.py` 的 `case03_source`
    fixture）。這裡刻意不用 pytest fixture（跨測試檔重複，而不是共用
    conftest 的一個），因為只有這個檔案的兩條測試需要它。
    """
    import tempfile

    tmp_root = tmp_root or Path(tempfile.mkdtemp())
    case_dir = tmp_root / "case03"
    (case_dir / "src").mkdir(parents=True, exist_ok=True)
    (case_dir / "data").mkdir(exist_ok=True)
    shutil.copy(
        corpus_root / "case03_sales_order" / "src" / "schema.sql", case_dir / "src" / "schema.sql"
    )
    shutil.copy(
        corpus_root / "case03_sales_order" / "data" / "seed.sql", case_dir / "data" / "seed.sql"
    )
    return case_dir


def test_sum_target_amount_from_odoo_json_floats_do_not_drift():
    """Odoo JSON-RPC 回應的數值欄位一樣是 JSON 浮點數，`json.loads` 讀回來
    一樣是 Python float——同一個地雷的另一半，`sum_target_amount` 也必須
    用 `Decimal(str(value))`。用 `json.loads` 讀出真正的 float（而不是
    在 Python 原始碼裡手寫字面值），模擬 client 真正拿到的資料形狀。
    """
    records = [json.loads(f'{{"price_subtotal": {v}}}') for v in ["0.1"] * 10]
    total = sum_target_amount(records)
    assert total == Decimal("1.0")


# --------------------------------------------------------------------------
# summary：從 checks 數出來，不另外累加計數器（見 report.py，這裡只確認
# Check 本身的欄位形狀符合 build_report 的期待）
# --------------------------------------------------------------------------


def test_summary_is_counted_from_checks_not_accumulated():
    """把一條 fail 的 check 加進去，summary 必須自己算出來。

    這條測試守的是計劃 2 那個 Critical 的形狀：統計若是另外累加的計數器，
    產出物少一項時分母不會跟著變，覆蓋率就會朝樂觀方向說謊。
    """
    from erp_migrai.pipeline.report import build_report

    checks = [
        Check("row_count", "a", "m", "pass", 3, 3, None, None),
        Check("row_count", "b", "n", "fail", 3, 2, None, None),
    ]
    report = build_report(checks, source_id="c3", synthetic=True)
    assert report["summary"] == {"total": 2, "passed": 1, "failed": 1}
    assert len(report["checks"]) == report["summary"]["total"]


# --------------------------------------------------------------------------
# 任務書 Step 2 最後一條測試：任務書自己承認斷言寫法很脆弱
# （`"/" not in ...` 會被任何含斜線的模型名絆倒），要求改寫成正面列舉允許
# 的鍵集合（原則 5.3：抄任務書給的測試碼是不夠的）。
# --------------------------------------------------------------------------


def test_report_has_no_timestamp_and_only_the_documented_keys():
    """報告必須可 diff：不含時間戳，且頂層／每筆 check 都只有文件承諾的鍵。

    正面列舉允許的鍵集合，而不是反面排除「含 `/` 的字串」——後者會被任何
    含斜線的模型名（例如未來若新增 `account.move.line`／`x_studio/foo`
    這類 studio 欄位）誤判成違規。
    """
    from erp_migrai.pipeline.report import build_report

    report = build_report(
        [Check("row_count", "a", "m", "pass", 1, 1, None, None)], source_id="c3", synthetic=True
    )
    assert set(report) == {"schema_version", "source_id", "synthetic", "checks", "summary"}
    assert set(report["checks"][0]) == {
        "kind",
        "source_table",
        "target_model",
        "status",
        "source_count",
        "target_count",
        "source_amount",
        "target_amount",
    }
    assert set(report["summary"]) == {"total", "passed", "failed"}
    blob = json.dumps(report, ensure_ascii=False)
    assert "timestamp" not in blob


# --------------------------------------------------------------------------
# count_names_with_prefix：純字串比對，不是 SQL LIKE
# --------------------------------------------------------------------------


def test_count_names_with_prefix_counts_matching_names():
    names = ["case03.cust_mast.C001", "case03.cust_mast.C002", "case03.ord_mast.O0001"]
    assert count_names_with_prefix(names, "case03.cust_mast.") == 2


def test_count_names_with_prefix_does_not_treat_underscore_as_wildcard():
    """來源表名常常含底線（`cust_mast`）。若這個函式的實作偷懶把 prefix
    組進 SQL `LIKE`／`=like` 樣式，`_` 會被資料庫解讀成「任一字元」而不是
    字面上的底線，`cust_mast` 這個前綴就可能誤配到 `custXmast` 這種完全
    不相干的表。純 python `str.startswith` 沒有這個問題——這條測試釘死
    「底線只能字面比對」這個行為，不是靠巧合通過。
    """
    names = ["case03.custXmast.C001", "case03.cust_mast.C001"]
    assert count_names_with_prefix(names, "case03.cust_mast.") == 1


def test_count_names_with_prefix_empty_list_is_zero():
    assert count_names_with_prefix([], "case03.cust_mast.") == 0


def test_count_names_with_prefix_requires_match_at_the_start_not_anywhere():
    """審查修正輪次 1 Important I2／M14：既有夾具（`custXmast` vs
    `cust_mast`）恰好分辨不出 `str.startswith` 跟 `prefix in name` 這兩種
    實作——兩者對那組夾具的判斷結果剛好一樣。這裡加一筆 prefix 出現在
    字串「中間」而不是「開頭」的名字：`str.startswith` 正確排除它，
    `prefix in name` 會誤判成命中。
    """
    names = ["x" + "case03.cust_mast.C001", "case03.cust_mast.C002"]
    assert count_names_with_prefix(names, "case03.cust_mast.") == 1


# --------------------------------------------------------------------------
# find_row_count_targets／find_amount_subtotal_target：從 mapping.json
# 找出要驗證什麼
# --------------------------------------------------------------------------


def _case03_mapping() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "corpus"
        / "case03_sales_order"
        / "expected"
        / "mapping.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_find_row_count_targets_on_case03_returns_all_four_tables_sorted():
    mapping = _case03_mapping()
    targets = find_row_count_targets(mapping)
    assert targets == [
        ("cust_mast", "res.partner"),
        ("ord_deta", "sale.order.line"),
        ("ord_mast", "sale.order"),
        ("prod_mast", "product.product"),
    ]


def test_find_amount_subtotal_target_on_case03_finds_ord_deta():
    mapping = _case03_mapping()
    result = find_amount_subtotal_target(mapping)
    assert result == ("ord_deta", "sale.order.line", "ord_qty", "ord_price")


def test_find_amount_subtotal_target_returns_none_when_no_line_table():
    """case01（沒有訂單明細）這種 mapping 應該回傳 `None`，而不是拋例外或
    湊出一個假答案——這正是「絕不悄悄產生錯的輸出」要擋的形狀。"""
    mapping = {
        "models": [{"source_table": "cust", "target_model": "res.partner"}],
        "mappings": [
            {
                "source": {"table": "cust", "column": "name"},
                "target": {"model": "res.partner", "field": "name"},
            }
        ],
    }
    assert find_amount_subtotal_target(mapping) is None


def test_find_amount_subtotal_target_requires_both_qty_and_price_fields():
    """只有 `product_uom_qty` 沒有 `price_unit`（或反過來）不該被誤判成
    找到了明細表——兩個欄位缺一不可，湊不出 qty × price。"""
    mapping = {
        "mappings": [
            {
                "source": {"table": "ord_deta", "column": "ord_qty"},
                "target": {"model": "sale.order.line", "field": "product_uom_qty"},
            }
        ]
    }
    assert find_amount_subtotal_target(mapping) is None


# --------------------------------------------------------------------------
# parse_migration_log：--log 是外部檔案，不能讓裸例外逃出去
# --------------------------------------------------------------------------


def _log_entry(**overrides: object) -> dict:
    base = {
        "run_id": "r1",
        "source_table": "cust_mast",
        "source_pk": ["C001"],
        "external_id": "migrai.case03.cust_mast.C001",
        "target_model": "res.partner",
        "action": "created",
        "odoo_id": 1,
        "error_code": None,
        "message": "",
    }
    base.update(overrides)
    return base


def test_parse_migration_log_parses_valid_lines():
    text = "\n".join(json.dumps(_log_entry()) for _ in range(3))
    entries = parse_migration_log(text)
    assert len(entries) == 3
    assert all(e["action"] == "created" for e in entries)


def test_parse_migration_log_empty_text_returns_empty_list():
    """空的 `migration_log.jsonl` 是合法狀態（例如載入階段一開始就因
    `plan_load` 失敗而一筆都沒寫），不該被當成格式錯誤。"""
    assert parse_migration_log("") == []
    assert parse_migration_log("\n\n") == []


def test_parse_migration_log_rejects_invalid_json_line():
    text = json.dumps(_log_entry()) + "\n{ 這不是合法的 JSON"
    with pytest.raises(MigrationLogReadError) as exc_info:
        parse_migration_log(text)
    assert "第 2 行" in str(exc_info.value)


def test_parse_migration_log_rejects_line_violating_schema():
    bad_entry = _log_entry(action="archived")  # 不在 ACTIONS 的四個值裡
    text = json.dumps(bad_entry)
    with pytest.raises(MigrationLogReadError):
        parse_migration_log(text)


def test_parse_migration_log_skips_blank_lines_between_valid_entries():
    text = json.dumps(_log_entry()) + "\n\n" + json.dumps(_log_entry(source_pk=["C002"]))
    entries = parse_migration_log(text)
    assert len(entries) == 2


# --------------------------------------------------------------------------
# find_mismatched_external_ids：--log 是否真的對應這次 --source-id
# （審查修正輪次 1 Critical C4／控制者裁決）
# --------------------------------------------------------------------------


def test_find_mismatched_external_ids_flags_entries_with_wrong_prefix():
    entries = [
        {"external_id": "migrai.case03.cust_mast.C001"},
        {"external_id": "migrai.other_source.cust_mast.C002"},
    ]
    result = find_mismatched_external_ids(entries, prefix="migrai.case03.")
    assert result == ["migrai.other_source.cust_mast.C002"]


def test_find_mismatched_external_ids_empty_when_all_match():
    entries = [
        {"external_id": "migrai.case03.cust_mast.C001"},
        {"external_id": "migrai.case03.ord_mast.O0001"},
    ]
    assert find_mismatched_external_ids(entries, prefix="migrai.case03.") == []


def test_find_mismatched_external_ids_empty_entries_is_empty_list():
    assert find_mismatched_external_ids([], prefix="migrai.case03.") == []


# --------------------------------------------------------------------------
# res_ids_with_prefix：`_target_line_amount` 用來過濾 res_id 的純函式
# （審查修正輪次 1 Important I3／M38）
# --------------------------------------------------------------------------


def test_res_ids_with_prefix_filters_by_prefix_and_ignores_other_source_ids():
    rows = [
        {"name": "case03.ord_deta.O0001-1", "res_id": 10},
        {"name": "case03.ord_deta.O0001-2", "res_id": 11},
        {"name": "other_source.ord_deta.O0001-1", "res_id": 99},
    ]
    assert res_ids_with_prefix(rows, "case03.ord_deta.") == [10, 11]


def test_res_ids_with_prefix_empty_rows_is_empty_list():
    assert res_ids_with_prefix([], "case03.ord_deta.") == []


# --------------------------------------------------------------------------
# ir_model_data_domain：查 ir.model.data 的 domain 一定要同時帶 module 與
# model 兩個條件（審查修正輪次 1 Important I3／M44）
# --------------------------------------------------------------------------


def test_ir_model_data_domain_includes_module_and_model_filters():
    domain = ir_model_data_domain(module="migrai", target_model="res.partner")
    assert ["module", "=", "migrai"] in domain
    assert ["model", "=", "res.partner"] in domain
    assert len(domain) == 2


# --------------------------------------------------------------------------
# resolve_synthetic：從 mapping.json 讀出 inventory_synthetic
# （審查修正輪次 1 Critical C1／M41）
# --------------------------------------------------------------------------


def test_resolve_synthetic_true():
    assert resolve_synthetic({"inventory_synthetic": True}) is True


def test_resolve_synthetic_false():
    """False 不能在傳遞路徑上被悄悄改成 True（M41 的字面重現）。"""
    assert resolve_synthetic({"inventory_synthetic": False}) is False


def test_resolve_synthetic_missing_key_defaults_to_false():
    assert resolve_synthetic({}) is False


# --------------------------------------------------------------------------
# resolve_target_counts：從「已查好的 name 清單」數出每張表的 target_count
# （審查修正輪次 1 Critical C1／M57）
# --------------------------------------------------------------------------


def test_resolve_target_counts_uses_source_id_and_table_prefix():
    """`names_by_model` 裡刻意混進『同一個模型、不同表』與『同一張表、
    不同 source_id』的名字——若 prefix 被換成空字串（M57：拿掉
    `source_id`／`table` 隔離）,兩張表都會把不該算進去的名字也數進去。
    """
    targets = [("cust_mast", "res.partner"), ("ord_mast", "sale.order")]
    names_by_model = {
        "res.partner": [
            "case03.cust_mast.C001",
            "case03.cust_mast.C002",
            "case03.ord_mast.O0001",  # 同一個 module，但是別的表
        ],
        "sale.order": [
            "case03.ord_mast.O0001",
            "case03.ord_mast.O0002",
            "other_source.ord_mast.O0003",  # 同一張表，但是別的 source_id
        ],
    }
    result = resolve_target_counts(targets, "case03", names_by_model)
    assert result == {"cust_mast": 2, "ord_mast": 2}


def test_resolve_target_counts_missing_model_in_names_by_model_is_zero():
    """`names_by_model` 沒有某個 target_model 的鍵（例如那個模型底下
    一筆 migrai 記錄都沒有）該當成 0，不該拋例外。"""
    targets = [("cust_mast", "res.partner")]
    result = resolve_target_counts(targets, "case03", {})
    assert result == {"cust_mast": 0}


# --------------------------------------------------------------------------
# assemble_checks：把已經查好的數字組成完整的 Check 清單
# （審查修正輪次 1 Critical C1，這一項解掉大半審查發現的缺口）
# --------------------------------------------------------------------------


def test_assemble_checks_combines_row_and_amount_checks_with_asymmetric_fixtures():
    """夾具全部非對稱：`source_counts`／`target_counts` 兩張表的數字都不
    相等，金額也不相等——分辨得出『兩個 count 對調』（M31）跟『金額
    check 永不產生』（M39）這兩種缺陷。
    """
    mapping = {
        "models": [
            {"source_table": "cust_mast", "target_model": "res.partner"},
            {"source_table": "ord_mast", "target_model": "sale.order"},
        ]
    }
    source_counts = {"cust_mast": 7, "ord_mast": 5}
    target_counts = {"cust_mast": 3, "ord_mast": 5}
    line_spec = ("ord_deta", "sale.order.line", "ord_qty", "ord_price")

    checks = assemble_checks(
        mapping,
        source_counts,
        target_counts,
        line_spec,
        Decimal("100.00"),
        Decimal("90.00"),
        rounding=Decimal("0.01"),
    )

    assert len(checks) == 3
    by_table = {c.source_table: c for c in checks}

    assert by_table["cust_mast"].kind == "row_count"
    assert by_table["cust_mast"].source_count == 7
    assert by_table["cust_mast"].target_count == 3
    assert by_table["cust_mast"].status == "fail"

    assert by_table["ord_mast"].kind == "row_count"
    assert by_table["ord_mast"].source_count == 5
    assert by_table["ord_mast"].target_count == 5
    assert by_table["ord_mast"].status == "pass"

    assert by_table["ord_deta"].kind == "amount_subtotal"
    assert by_table["ord_deta"].source_amount == "100.00"
    assert by_table["ord_deta"].target_amount == "90.00"
    assert by_table["ord_deta"].status == "fail"


def test_assemble_checks_no_amount_check_when_line_spec_is_none():
    mapping = {"models": [{"source_table": "cust", "target_model": "res.partner"}]}
    checks = assemble_checks(mapping, {"cust": 3}, {"cust": 3}, None, None, None)
    assert len(checks) == 1
    assert checks[0].kind == "row_count"


def test_assemble_checks_uses_verification_rounding_as_default_tolerance():
    """不明講 `rounding` 時必須用 `VERIFICATION_ROUNDING`（0.01），不是被
    放大的容差（M42：容差放大到 1000 倍，一千元差額也算 pass）——這裡的
    50 元差額在 0.01 容差下必須 `fail`，只有容差被放大好幾個數量級才會
    變成 `pass`。
    """
    checks = assemble_checks(
        {"models": []},
        {},
        {},
        ("ord_deta", "sale.order.line", "ord_qty", "ord_price"),
        Decimal("100.00"),
        Decimal("150.00"),
    )
    assert len(checks) == 1
    assert checks[0].status == "fail"


def test_assemble_checks_rejects_line_spec_without_amounts():
    """`line_spec` 非 `None` 卻沒帶 `source_amount`／`target_amount` 是
    呼叫端的契約違反，必須大聲失敗，不能悄悄跳過金額比對。"""
    with pytest.raises(ValueError):
        assemble_checks(
            {"models": []},
            {},
            {},
            ("ord_deta", "sale.order.line", "ord_qty", "ord_price"),
            None,
            None,
        )


# --------------------------------------------------------------------------
# fatal_code_for：row_count → E-5001，amount_subtotal → E-5002
# （審查修正輪次 1 Critical C2／M34）
# --------------------------------------------------------------------------


def test_fatal_code_for_row_count_check_is_e5001():
    c = compare_row_count("cust_mast", "res.partner", 3, 2)
    assert fatal_code_for(c) is E_5001


def test_fatal_code_for_amount_check_is_e5002():
    c = compare_amount(
        "ord_deta", "sale.order.line", Decimal("1.00"), Decimal("5.00"), rounding=Decimal("0.01")
    )
    assert fatal_code_for(c) is E_5002


# --------------------------------------------------------------------------
# VERIFICATION_ROUNDING／TARGET_LINE_AMOUNT_FIELD：兩個原本沒有測試守著
# 的常數（審查修正輪次 1 Important I4／I3）
# --------------------------------------------------------------------------


def test_verification_rounding_is_one_cent():
    assert Decimal("0.01") == VERIFICATION_ROUNDING


def test_target_line_amount_field_is_price_subtotal_not_price_total():
    """『比未稅 price_subtotal 而非含稅』是這個模組存在的理由——見模組
    docstring「為什麼比對 price_subtotal 而不是 amount_total」那一節。
    """
    assert TARGET_LINE_AMOUNT_FIELD == "price_subtotal"
