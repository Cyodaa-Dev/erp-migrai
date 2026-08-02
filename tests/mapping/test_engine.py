"""engine.build_mapping 的測試。

涵蓋 task-6-brief.md「必要測試案例」一節列出的每一條，並依 ruling 1 更正第二條
（規則目錄為空的 case01：cust_mast 對每個內建模型最後一段的 name_score 都是
0.0，所以走的是「完全對不上模型」分支，而不是欄位層級的 name_score 分支）。

另外補上 brief 行為規格提到、但沒有列進必要測試案例的分支（表級平手、規則
目標模型不存在於 catalog、各種畸形 inventory 輸入），以及 ruling 7 要求的
「每個來源欄位恰好落在 mappings／unmapped／conflicts 三者之一」稽核測試。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import erp_migrai.mapping as mapping_package
from erp_migrai.events import EventLog, Stage
from erp_migrai.mapping.catalog import OdooField, OdooModel, load_catalog
from erp_migrai.mapping.engine import MapFailed, _resolve_duplicate_model_mappings, build_mapping
from erp_migrai.mapping.models import (
    ConflictKind,
    ModelMapping,
    Tier,
    Transform,
    TransformKind,
)
from erp_migrai.mapping.rules_loader import ColumnRule, RuleSet, TableRule, load_rules

CATALOG = load_catalog()
RULES = load_rules(CATALOG)
EMPTY_RULES = RuleSet(table_rules={}, column_rules={})

_CORPUS_ROOT = Path(__file__).resolve().parents[2] / "corpus"
INVENTORY_PATH = _CORPUS_ROOT / "case01_partner" / "expected" / "inventory.json"
_SCHEMA_PATH = Path(mapping_package.__file__).parent / "schema" / "mapping.schema.json"


def _validate_against_schema(payload: dict) -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)


def _case01_inventory() -> dict:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _log(tmp_path: Path) -> EventLog:
    return EventLog(path=tmp_path / "events.jsonl", run_id="test")


def _table(
    name: str, columns: list[tuple[str, str]], *, foreign_keys: list[dict] | None = None
) -> dict:
    return {
        "name": name,
        "columns": [
            {"name": col_name, "type": col_type, "nullable": True, "default": None}
            for col_name, col_type in columns
        ],
        "primary_key": [],
        "foreign_keys": foreign_keys if foreign_keys is not None else [],
    }


def _inventory(tables: list[dict], *, synthetic: bool = True) -> dict:
    return {
        "synthetic": synthetic,
        "schema_version": "1.0",
        "database": None,
        "source_files": [],
        "tables": tables,
        "programs": [],
        "unsupported_syntax": [],
        "database_conflicts": [],
    }


def _accounted_columns(mapping) -> Counter[tuple[str, str]]:
    """回傳每個 `(table, column)` 鍵在 mappings／unmapped／conflicts 三者合計
    出現的次數，用於稽核每個來源欄位都恰好被算到一次（修正輪次 2 Minor 4：
    改用 `Counter` 而非 `set`——`set` 會把兩個同名的欄位摺疊成同一個鍵，
    若其中一個在處理過程中被悄悄漏掉，`set` 版本的稽核完全看不出來；
    `Counter` 才能檢查「恰好幾次」，而不只是「有沒有出現過」）。
    """
    counts: Counter[tuple[str, str]] = Counter()
    for fm in mapping.mappings:
        counts[(fm.source.table, fm.source.column)] += 1
    for u in mapping.unmapped:
        counts[(u.table, u.column)] += 1
    for c in mapping.conflicts:
        if c.kind == ConflictKind.COLUMN:  # 表級衝突的 column 是 None，不是一個真的來源欄位
            counts[(c.table, c.column)] += 1
    return counts


# ---------------------------------------------------------------------------
# 必要測試案例
# ---------------------------------------------------------------------------


def test_case01_with_builtin_catalog_and_rules_maps_all_seven_columns(tmp_path: Path):
    inventory = _case01_inventory()
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, RULES, event_log=log)

    assert len(mapping.mappings) == 7
    assert all(fm.confidence == 100 for fm in mapping.mappings)
    assert all(fm.tier == Tier.AUTO for fm in mapping.mappings)
    assert mapping.unmapped == []
    assert mapping.conflicts == []
    assert mapping.stats.coverage_percent == 100
    assert mapping.stats.auto == 7
    assert mapping.stats.suggested == 0
    assert mapping.stats.needs_human == 0
    assert not log.has_fatal(Stage.MAP)


def test_case01_with_empty_rules_all_seven_columns_land_in_unmapped_no_model(tmp_path: Path):
    """ruling 1 更正版：規則目錄為空時，cust_mast 對每個內建模型最後一段的
    name_score 都是 0.0（partner／template／order／line 沒有任何共同 token），
    所以走的是表→模型 spec 第 3 條「完全對不上」，不是欄位層級的 name_score
    分支。因此不應只斷言 brief 原文點名的三欄，而要斷言完整的真相：不產生任何
    ModelMapping，7 個欄位全部進 unmapped，理由是表級訊息而非 name_score 理由。
    """
    inventory = _case01_inventory()
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, EMPTY_RULES, event_log=log)

    assert mapping.models == []
    assert mapping.mappings == []
    assert mapping.conflicts == []
    assert len(mapping.unmapped) == 7
    assert {u.column for u in mapping.unmapped} == {
        "cust_no",
        "cust_name",
        "cust_ubn",
        "cust_tel",
        "cust_addr",
        "cust_credit",
        "cust_stus",
    }
    for u in mapping.unmapped:
        assert u.reason == "來源表找不到對應的 Odoo 模型"
    assert mapping.stats.coverage_percent == 0

    e2002_events = [e for e in log.events if e["error_code"] == "E-2002"]
    assert len(e2002_events) == 1
    assert not log.has_fatal(Stage.MAP)


def test_inventory_synthetic_flag_passes_through_unchanged(tmp_path: Path):
    for flag in (True, False):
        inventory = _inventory([], synthetic=flag)
        mapping = build_mapping(inventory, CATALOG, EMPTY_RULES, event_log=_log(tmp_path))
        assert mapping.inventory_synthetic is flag


def test_column_rule_pointing_to_different_model_than_table_is_conflict_and_fatal(
    tmp_path: Path,
):
    rules = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast")},
        column_rules={
            ("cust_mast", "cust_no"): ColumnRule(
                source_table="cust_mast",
                source_column="cust_no",
                target_model="product.template",
                target_field="name",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#cust_mast.cust_no",
            )
        },
    )
    inventory = _inventory([_table("cust_mast", [("cust_no", "CHAR(10)")])])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, rules, event_log=log)

    assert len(mapping.conflicts) == 1
    conflict = mapping.conflicts[0]
    assert conflict.table == "cust_mast"
    assert conflict.column == "cust_no"
    assert conflict.kind == ConflictKind.COLUMN
    assert mapping.mappings == []

    e2001_events = [e for e in log.events if e["error_code"] == "E-2001"]
    assert len(e2001_events) == 1
    assert log.has_fatal(Stage.MAP)


def test_two_source_columns_ruled_to_same_target_field_produce_target_conflict(
    tmp_path: Path,
):
    """Task 4.5（昴君裁決）：同一張來源表裡，兩個不同的來源欄位都命中規則、
    對應到同一個目標欄位（`sale.order.client_order_ref`）——到了載入階段，
    同一筆記錄的這個欄位會被後寫的覆蓋，使用者無從得知哪一個贏，這本質上
    就是映射衝突，必須記進 `conflicts[]` 並發 `E-2001`。

    兩筆 `FieldMapping` 仍然都要保留在 `mappings` 裡——衝突是「該用哪一個」
    的裁決問題，不是任一筆本身算錯了，不能因為偵測到衝突就悄悄丟掉其中
    一筆已經算出來的事實。
    """
    rules = RuleSet(
        table_rules={"ord_mast": TableRule("ord_mast", "sale.order", "test#ord_mast")},
        column_rules={
            ("ord_mast", "ord_no"): ColumnRule(
                source_table="ord_mast",
                source_column="ord_no",
                target_model="sale.order",
                target_field="client_order_ref",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#ord_mast.ord_no",
            ),
            ("ord_mast", "ord_cust_ref"): ColumnRule(
                source_table="ord_mast",
                source_column="ord_cust_ref",
                target_model="sale.order",
                target_field="client_order_ref",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#ord_mast.ord_cust_ref",
            ),
        },
    )
    inventory = _inventory(
        [_table("ord_mast", [("ord_no", "CHAR(16)"), ("ord_cust_ref", "VARCHAR(40)")])]
    )
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, rules, event_log=log)

    assert len(mapping.mappings) == 2
    assert {fm.source.column for fm in mapping.mappings} == {"ord_no", "ord_cust_ref"}

    assert len(mapping.conflicts) == 1
    conflict = mapping.conflicts[0]
    assert conflict.table == "ord_mast"
    assert conflict.column is None
    assert conflict.kind == ConflictKind.TARGET
    assert conflict.candidates == ["ord_cust_ref", "ord_no"]  # 字典序，確定性
    assert "sale.order.client_order_ref" in conflict.reason
    assert "覆蓋" in conflict.reason

    e2001_events = [e for e in log.events if e["error_code"] == "E-2001"]
    assert len(e2001_events) == 1
    assert log.has_fatal(Stage.MAP)

    _validate_against_schema(mapping.to_dict())


def test_target_conflict_spans_auto_and_needs_human_tiers_like_case03(tmp_path: Path):
    """跨 tier 都要抓：這裡直接用內建 catalog／規則重現 corpus case03 實際
    揭露的形狀——`ord_mast.ord_no` 命中規則（confidence 100／auto），
    `ord_mast.ord_cust_ref` 沒有規則、落回名稱／型別啟發式
    （confidence 30／needs_human），兩者都對到 `sale.order.client_order_ref`。
    不能因為兩者信心層級不同就放過這組衝突。
    """
    inventory = _inventory(
        [_table("ord_mast", [("ord_no", "CHAR(16)"), ("ord_cust_ref", "VARCHAR(40)")])]
    )
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, RULES, event_log=log)

    by_column = {fm.source.column: fm for fm in mapping.mappings}
    assert by_column["ord_no"].tier == Tier.AUTO
    assert by_column["ord_no"].confidence == 100
    assert by_column["ord_cust_ref"].tier == Tier.NEEDS_HUMAN
    assert by_column["ord_cust_ref"].confidence == 30
    assert by_column["ord_no"].target.field == "client_order_ref"
    assert by_column["ord_cust_ref"].target.field == "client_order_ref"

    assert len(mapping.conflicts) == 1
    conflict = mapping.conflicts[0]
    assert conflict.kind == ConflictKind.TARGET
    assert conflict.table == "ord_mast"
    assert conflict.column is None
    assert conflict.candidates == ["ord_cust_ref", "ord_no"]

    e2001_events = [e for e in log.events if e["error_code"] == "E-2001"]
    assert len(e2001_events) == 1
    assert log.has_fatal(Stage.MAP)


def test_two_different_source_tables_targeting_same_field_is_not_a_conflict(tmp_path: Path):
    """只在同一張來源表內比對——不同來源表對到同一個 Odoo 欄位是正常的，
    它們會變成不同的記錄，不該被誤報成 TARGET 衝突。"""
    rules = RuleSet(
        table_rules={
            "tbl_a": TableRule("tbl_a", "res.partner", "test#tbl_a"),
            "tbl_b": TableRule("tbl_b", "product.template", "test#tbl_b"),
        },
        column_rules={
            ("tbl_a", "col_a"): ColumnRule(
                source_table="tbl_a",
                source_column="col_a",
                target_model="res.partner",
                target_field="name",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#tbl_a.col_a",
            ),
            ("tbl_b", "col_b"): ColumnRule(
                source_table="tbl_b",
                source_column="col_b",
                target_model="product.template",
                target_field="name",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#tbl_b.col_b",
            ),
        },
    )
    inventory = _inventory(
        [_table("tbl_a", [("col_a", "CHAR(10)")]), _table("tbl_b", [("col_b", "CHAR(10)")])]
    )
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, rules, event_log=log)

    assert len(mapping.mappings) == 2
    assert mapping.conflicts == []
    assert not log.has_fatal(Stage.MAP)


def test_two_catalog_fields_tied_at_same_confidence_produce_conflict_not_mapping(
    tmp_path: Path,
):
    # `no_a`／`no_b` 正規化後都與來源 `no` 有交集且來源是子集，name_score 都是
    # 0.8；型別同為 char 對 char，type_score 都是 1.0；兩邊乘出來都是 80，
    # 剛好並列。
    catalog = {
        "x.y": OdooModel(
            name="x.y",
            description="測試用模型",
            fields={
                "no_a": OdooField(name="no_a", type="char", required=True, module="base", help="a"),
                "no_b": OdooField(
                    name="no_b", type="char", required=False, module="base", help="b"
                ),
            },
        )
    }
    rules = RuleSet(
        table_rules={"tied_tbl": TableRule("tied_tbl", "x.y", "test#tied_tbl")},
        column_rules={},
    )
    inventory = _inventory([_table("tied_tbl", [("no", "CHAR(10)")])])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert mapping.mappings == []
    assert len(mapping.conflicts) == 1
    conflict = mapping.conflicts[0]
    assert conflict.table == "tied_tbl"
    assert conflict.column == "no"
    assert conflict.kind == ConflictKind.COLUMN
    assert conflict.candidates == ["x.y.no_a", "x.y.no_b"]


def test_type_needs_conversion_but_name_identical_is_needs_human_not_unmapped(tmp_path: Path):
    # cust_mast.active 型別 CHAR(1)，target res.partner.active 是 boolean：
    # name_score 1.0 × type_score 0.5 → confidence 50 → needs_human，而不是
    # 找不到對應（unmapped）。對得上但需要人確認，與完全對不上是兩件事。
    rules = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast")},
        column_rules={},
    )
    inventory = _inventory([_table("cust_mast", [("active", "CHAR(1)")])])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, rules, event_log=log)

    assert mapping.unmapped == []
    assert mapping.conflicts == []
    assert len(mapping.mappings) == 1
    fm = mapping.mappings[0]
    assert fm.target.model == "res.partner"
    assert fm.target.field == "active"
    assert fm.confidence == 50
    assert fm.tier == Tier.NEEDS_HUMAN


def test_empty_inventory_no_tables_returns_empty_mapping_with_full_coverage(tmp_path: Path):
    inventory = _inventory([])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, RULES, event_log=log)

    assert mapping.models == []
    assert mapping.mappings == []
    assert mapping.unmapped == []
    assert mapping.conflicts == []
    assert mapping.stats.coverage_percent == 100
    assert mapping.stats.source_columns == 0
    assert not any(e["error_code"] == "E-2002" for e in log.events)


def test_inventory_missing_tables_key_raises_map_failed(tmp_path: Path):
    inventory = _case01_inventory()
    del inventory["tables"]
    log = _log(tmp_path)

    with pytest.raises(MapFailed):
        build_mapping(inventory, CATALOG, RULES, event_log=log)

    assert any(e["error_code"] == "E-2001" for e in log.events)


def test_every_field_mapping_and_model_mapping_has_nonempty_reasons(tmp_path: Path):
    inventory = _case01_inventory()
    mapping = build_mapping(inventory, CATALOG, RULES, event_log=_log(tmp_path))

    assert mapping.mappings  # 確保這條測試真的走過至少一筆
    assert all(fm.reasons for fm in mapping.mappings)
    # Important 5：brief 明訂 `FieldMapping` 與 `ModelMapping` 的 reasons 都要非空，
    # 但這裡先前只斷言了 mappings，models 那半邊完全沒被驗證——一個
    # `ModelMapping.reasons == []` 的實作會通過這條測試，卻會被
    # `mapping.schema.json` 的 `minItems: 1` 在寫檔時打回票。
    assert mapping.models
    assert all(mm.reasons for mm in mapping.models)


def test_build_mapping_is_deterministic_across_two_calls(tmp_path: Path):
    inventory = _case01_inventory()

    mapping_1 = build_mapping(inventory, CATALOG, RULES, event_log=_log(tmp_path / "a"))
    mapping_2 = build_mapping(inventory, CATALOG, RULES, event_log=_log(tmp_path / "b"))

    assert mapping_1.to_dict() == mapping_2.to_dict()


# ---------------------------------------------------------------------------
# 表→模型 spec 的其餘分支：無規則時用 name_score 找唯一最高分模型、以及表級平手
# ---------------------------------------------------------------------------


def test_no_table_rule_uses_name_score_against_model_last_segment(tmp_path: Path):
    # 表名 "partner_master" 正規化後與 res.partner 的最後一段 "partner" 有
    # 交集且是子集，其餘模型的最後一段（template／order／line）都沒有交集，
    # 所以 res.partner 是唯一最高分。
    inventory = _inventory([_table("partner_master", [])])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, EMPTY_RULES, event_log=log)

    assert len(mapping.models) == 1
    model_mapping = mapping.models[0]
    assert model_mapping.source_table == "partner_master"
    assert model_mapping.target_model == "res.partner"
    assert model_mapping.reasons


def test_table_level_tie_produces_conflict_and_columns_land_in_unmapped(tmp_path: Path):
    # 兩個模型的最後一段都叫 "order"，對表名 "order" 而言 name_score 都是
    # 1.0，confidence 都是 100，剛好並列——不產生 ModelMapping，該表所有欄位
    # 進 unmapped（而不是靜默消失），並且這筆表級平手本身也算一筆 conflict。
    catalog = {
        "a.order": OdooModel(
            name="a.order",
            description="測試用",
            fields={"f": OdooField(name="f", type="char", required=True, module="base", help="h")},
        ),
        "b.order": OdooModel(
            name="b.order",
            description="測試用",
            fields={"f": OdooField(name="f", type="char", required=True, module="base", help="h")},
        ),
    }
    inventory = _inventory([_table("order", [("col1", "CHAR(1)"), ("col2", "CHAR(1)")])])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, EMPTY_RULES, event_log=log)

    assert mapping.models == []
    assert mapping.mappings == []
    assert len(mapping.conflicts) == 1
    tie_conflict = mapping.conflicts[0]
    assert tie_conflict.table == "order"
    # 修正輪次 1 Important 3：表級衝突用 `kind=TABLE`、`column=None` 當判別
    # 欄位，不再用 `column == ""` 當哨兵值（那會讓遵循 schema 的消費者把
    # 表級衝突誤讀成「有一個名字叫空字串的欄位衝突」）。
    assert tie_conflict.kind == ConflictKind.TABLE
    assert tie_conflict.column is None
    assert tie_conflict.candidates == ["a.order", "b.order"]

    assert len(mapping.unmapped) == 2
    assert {u.column for u in mapping.unmapped} == {"col1", "col2"}
    # Minor 10：先前這裡從未斷言表級平手的 unmapped 理由字串，而那正是這條
    # 被派生決定的重點所在。
    assert all(
        u.reason == "來源表對應的 Odoo 模型評分並列，未產生 ModelMapping，欄位無法比對"
        for u in mapping.unmapped
    )

    # 表級平手也要算一筆 E-2001。
    assert any(e["error_code"] == "E-2001" for e in log.events)
    # 但 stats.source_columns 只算真實欄位，不把表級平手本身算成一欄。
    assert mapping.stats.source_columns == 2


# ---------------------------------------------------------------------------
# 絕不拋出：各種畸形輸入
# ---------------------------------------------------------------------------


def test_column_with_missing_type_does_not_raise(tmp_path: Path):
    inventory = _inventory(
        [
            {
                "name": "cust_mast",
                "columns": [{"name": "mystery_col", "nullable": True, "default": None}],
                "primary_key": [],
                "foreign_keys": [],
            }
        ]
    )
    rules = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast")},
        column_rules={},
    )
    mapping = build_mapping(inventory, CATALOG, rules, event_log=_log(tmp_path))

    assert len(mapping.unmapped) == 1
    assert mapping.unmapped[0].column == "mystery_col"
    assert mapping.unmapped[0].type == ""


def test_column_with_null_type_does_not_raise(tmp_path: Path):
    inventory = _inventory(
        [
            {
                "name": "cust_mast",
                "columns": [
                    {
                        "name": "mystery_col",
                        "type": None,
                        "nullable": True,
                        "default": None,
                    }
                ],
                "primary_key": [],
                "foreign_keys": [],
            }
        ]
    )
    rules = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast")},
        column_rules={},
    )
    mapping = build_mapping(inventory, CATALOG, rules, event_log=_log(tmp_path))

    assert len(mapping.unmapped) == 1
    assert mapping.unmapped[0].type == ""


def test_table_with_no_columns_does_not_raise(tmp_path: Path):
    rules = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast")},
        column_rules={},
    )
    inventory = _inventory([_table("cust_mast", [])])
    mapping = build_mapping(inventory, CATALOG, rules, event_log=_log(tmp_path))

    assert len(mapping.models) == 1
    assert mapping.mappings == []
    assert mapping.unmapped == []
    assert mapping.stats.source_columns == 0
    assert mapping.stats.coverage_percent == 100


def test_duplicated_table_name_does_not_raise_and_processes_both(tmp_path: Path):
    rules = RuleSet(
        table_rules={"dup": TableRule("dup", "res.partner", "test#dup")},
        column_rules={},
    )
    inventory = _inventory(
        [
            _table("dup", [("cola", "CHAR(1)")]),
            _table("dup", [("colb", "CHAR(1)")]),
        ]
    )
    mapping = build_mapping(inventory, CATALOG, rules, event_log=_log(tmp_path))

    columns_seen = {u.column for u in mapping.unmapped} | {
        fm.source.column for fm in mapping.mappings
    }
    # Important 6：原本的斷言用 `or`，一個引擎只處理了兩筆重複表其中一筆的
    # 實作也會通過，卻違背測試名稱承諾的「both」。兩張表名相同、規則相同，
    # 各自的欄位（cola／colb）都必須出現。
    assert columns_seen == {"cola", "colb"}
    assert mapping.stats.source_columns == 2
    # 修正輪次 3 Critical 1：兩筆表項各自獨立呼叫 `_resolve_target_model`，
    # 命中同一條 table rule，各自產生一筆內容完全相同的 `ModelMapping`——
    # 這不是矛盾，只是同一件事實被記錄了兩次，`build_mapping` 現在會把它們
    # 去重成一筆，而不是讓消費者拿到兩筆鍵完全相同、卻無法區分是誰對應到誰
    # 的 `ModelMapping`。
    assert len(mapping.models) == 1
    assert mapping.models[0].source_table == "dup"
    assert mapping.models[0].target_model == "res.partner"


def test_non_ascii_table_and_column_names_do_not_raise(tmp_path: Path):
    rules = RuleSet(
        table_rules={"客戶主檔": TableRule("客戶主檔", "res.partner", "test#客戶主檔")},
        column_rules={
            ("客戶主檔", "客戶編號"): ColumnRule(
                source_table="客戶主檔",
                source_column="客戶編號",
                target_model="res.partner",
                target_field="ref",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#客戶主檔.客戶編號",
            )
        },
    )
    inventory = _inventory([_table("客戶主檔", [("客戶編號", "CHAR(10)")])])
    mapping = build_mapping(inventory, CATALOG, rules, event_log=_log(tmp_path))

    assert len(mapping.mappings) == 1
    assert mapping.mappings[0].source.table == "客戶主檔"
    assert mapping.mappings[0].source.column == "客戶編號"


def test_malformed_table_entry_that_is_not_a_dict_does_not_raise(tmp_path: Path):
    inventory = _inventory(["not_a_table_dict", 42, None])
    mapping = build_mapping(inventory, CATALOG, EMPTY_RULES, event_log=_log(tmp_path))

    assert mapping.models == []
    assert mapping.mappings == []
    assert mapping.unmapped == []
    assert mapping.conflicts == []


def test_inventory_that_is_not_a_dict_raises_map_failed(tmp_path: Path):
    with pytest.raises(MapFailed):
        build_mapping(None, CATALOG, RULES, event_log=_log(tmp_path))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ruling 2：stats 必須與實際產出的清單一致，不能靠平行計數器
# ---------------------------------------------------------------------------


def test_stats_tier_counts_sum_to_len_mappings(tmp_path: Path):
    inventory = _case01_inventory()
    mapping = build_mapping(inventory, CATALOG, RULES, event_log=_log(tmp_path))

    assert mapping.stats.auto + mapping.stats.suggested + mapping.stats.needs_human == len(
        mapping.mappings
    )


# ---------------------------------------------------------------------------
# ruling 7：每個來源欄位必須恰好落在 mappings／unmapped／conflicts 三者之一
# ---------------------------------------------------------------------------


def _all_column_keys(inventory: dict) -> Counter[tuple[str, str]]:
    """跟 `_accounted_columns` 對稱：同一張表兩個同名欄位要算兩次，而不是
    摺疊成一個鍵（修正輪次 2 Minor 4）。"""
    counts: Counter[tuple[str, str]] = Counter()
    for table in inventory["tables"]:
        for column in table["columns"]:
            counts[(table["name"], column["name"])] += 1
    return counts


@pytest.mark.parametrize("rules_used", [RULES, EMPTY_RULES])
def test_every_source_column_in_case01_is_accounted_for_exactly_once(tmp_path: Path, rules_used):
    inventory = _case01_inventory()
    mapping = build_mapping(inventory, CATALOG, rules_used, event_log=_log(tmp_path))

    expected = _all_column_keys(inventory)
    accounted = _accounted_columns(mapping)

    assert accounted == expected


def test_every_source_column_in_mixed_synthetic_scenario_is_accounted_for_exactly_once(
    tmp_path: Path,
):
    # 混合：一個完全對不上模型的表、一個有欄位規則衝突的表、一個型別需轉換的表、
    # 一個欄位並列的表，確保各種分支同時發生時，稽核仍然成立。
    catalog = {
        "x.y": OdooModel(
            name="x.y",
            description="測試用模型",
            fields={
                "no_a": OdooField(name="no_a", type="char", required=True, module="base", help="a"),
                "no_b": OdooField(
                    name="no_b", type="char", required=False, module="base", help="b"
                ),
            },
        ),
        **CATALOG,
    }
    rules = RuleSet(
        table_rules={
            "cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast"),
            "tied_tbl": TableRule("tied_tbl", "x.y", "test#tied_tbl"),
        },
        column_rules={
            ("cust_mast", "cust_no"): ColumnRule(
                source_table="cust_mast",
                source_column="cust_no",
                target_model="product.template",  # 與表模型不一致 → conflict
                target_field="name",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#cust_mast.cust_no",
            )
        },
    )
    inventory = _inventory(
        [
            # 修正輪次 2 Minor 4：`dup_col` 在這張表裡出現兩次（同名重複欄位），
            # 兩筆都會各自落進 `unmapped`（表本身對不上模型）；用 `Counter`
            # 稽核才能檢查「恰好兩次」，`set` 版本會把兩者摺疊成一個鍵，看不出
            # 其中一筆若被悄悄漏掉。兩次出現的型別刻意不同（`CHAR(1)`／
            # `CHAR(2)`）——最終整分支審查修正 Important 1 之後，`unmapped`
            # 也會把「完整內容逐位元組相同」的重複收斂成一筆（跟 `models`／
            # `mappings` 一致），型別不同讓這兩筆 `UnmappedColumn` 保持
            # 「內容不同」，才測得出這裡真正要守住的東西：兩個同名但實際上
            # 不同的來源欄位都必須被個別看見，而不是被去重機制誤傷。
            _table(
                "zzz_no_such_table_zzz",
                [("weird_col", "CHAR(1)"), ("dup_col", "CHAR(1)"), ("dup_col", "CHAR(2)")],
            ),
            _table("cust_mast", [("cust_no", "CHAR(10)"), ("active", "CHAR(1)")]),
            _table("tied_tbl", [("no", "CHAR(10)")]),
        ]
    )
    log = _log(tmp_path)
    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    expected = _all_column_keys(inventory)
    accounted = _accounted_columns(mapping)

    assert accounted == expected
    assert accounted[("zzz_no_such_table_zzz", "dup_col")] == 2
    # Minor 10：先前從未斷言這裡的兩筆 conflict（cust_mast.cust_no 規則衝突、
    # tied_tbl.no 欄位並列）各自都發了一個 E-2001，只驗證了「欄位有落點」，
    # 沒驗證「衝突真的被回報成事件」。
    e2001_events = [e for e in log.events if e["error_code"] == "E-2001"]
    assert len(e2001_events) == 2


# ---------------------------------------------------------------------------
# 修正輪次 1 Critical 1：schema-valid 但 name 缺失／非字串的欄位不能悄悄消失
# ---------------------------------------------------------------------------


def test_schema_valid_empty_named_column_is_accounted_for_and_coverage_is_honest(
    tmp_path: Path,
):
    """`inventory.schema.json` 的 `$defs/column.name` 只要求 `string`，沒有
    `minLength`，所以 `{"name": ""}` 是完全合法、`jsonschema` 會放行的輸入。

    controller 重現過的缺陷：這種欄位以前在 `_normalize_columns` 被直接跳過，
    不進 `mappings`／`unmapped`／`conflicts` 任何一個清單，`_build_stats` 又
    是從這三個清單「數出來」`source_columns`，於是連分母都跟著漏，
    `coverage_percent` 因此虛報成 100%（實際上只有 2/3 欄位有意義）。
    """
    rules = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast")},
        column_rules={},
    )
    inventory = _inventory(
        [
            {
                "name": "cust_mast",
                "columns": [
                    {"name": "cust_name", "type": "CHAR(60)", "nullable": True, "default": None},
                    {"name": "cust_tel", "type": "CHAR(20)", "nullable": True, "default": None},
                    {"name": "", "type": "CHAR(10)", "nullable": True, "default": None},
                ],
                "primary_key": [],
                "foreign_keys": [],
            }
        ]
    )
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, rules, event_log=log)

    assert mapping.stats.source_columns == 3

    outputs_containing_empty_name = [
        (fm.source.table, fm.source.column) for fm in mapping.mappings if fm.source.column == ""
    ] + [(u.table, u.column) for u in mapping.unmapped if u.column == ""]
    assert len(outputs_containing_empty_name) == 1
    assert outputs_containing_empty_name[0] == ("cust_mast", "")

    empty_named = next(u for u in mapping.unmapped if u.column == "")
    assert empty_named.reason == "欄位名稱缺失或不是字串，無法比對"
    assert empty_named.type == "CHAR(10)"

    # 兩個合法欄位（cust_name／cust_tel）對得上 res.partner，真實覆蓋率是
    # round(100 * 2 / 3) == 67，而不是失守前虛報的 100。
    assert mapping.stats.coverage_percent == 67


def test_non_string_column_name_uses_repr_so_original_value_is_visible(tmp_path: Path):
    rules = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast")},
        column_rules={},
    )
    inventory = _inventory(
        [
            {
                "name": "cust_mast",
                "columns": [
                    {"name": 123, "type": "CHAR(10)", "nullable": True, "default": None},
                    {"name": None, "type": "CHAR(10)", "nullable": True, "default": None},
                ],
                "primary_key": [],
                "foreign_keys": [],
            }
        ]
    )
    mapping = build_mapping(inventory, CATALOG, rules, event_log=_log(tmp_path))

    assert mapping.stats.source_columns == 2
    columns = {u.column for u in mapping.unmapped}
    assert columns == {"123", "None"}
    assert all(u.reason == "欄位名稱缺失或不是字串，無法比對" for u in mapping.unmapped)


# ---------------------------------------------------------------------------
# 修正輪次 1 Important 2：四個排序鍵補到全序，輸入順序不再影響輸出
# ---------------------------------------------------------------------------


def test_reordering_duplicate_tables_in_input_produces_byte_identical_output(tmp_path: Path):
    """兩個 inventory 只差在「兩張同名表的輸入順序」，其餘完全相同——
    修正前，四個排序鍵都只取部分欄位，靠 stable sort + 輸入順序恰好一致才
    看起來確定；這裡故意對調順序，驗證 `to_dict()` 現在真的逐位元組相同。
    """
    rules = RuleSet(
        table_rules={"dup": TableRule("dup", "res.partner", "test#dup")},
        column_rules={},
    )
    table_a = _table("dup", [("cust_no", "CHAR(10)")])
    table_b = _table("dup", [("cust_tel", "CHAR(20)")])

    inventory_forward = _inventory([table_a, table_b])
    inventory_reversed = _inventory([table_b, table_a])

    mapping_forward = build_mapping(
        inventory_forward, CATALOG, rules, event_log=_log(tmp_path / "forward")
    )
    mapping_reversed = build_mapping(
        inventory_reversed, CATALOG, rules, event_log=_log(tmp_path / "reversed")
    )

    assert mapping_forward.to_dict() == mapping_reversed.to_dict()


def test_reordering_duplicate_tables_with_coercion_note_produces_byte_identical_output(
    tmp_path: Path,
):
    """修正輪次 2 Important 1 的具體重現：一張表原始 `name` 是整數 `42`、
    另一張是字串 `"42"`，兩者都被 `_process_table` 正規化成同一個
    `table_name == "42"`，都命中同一條表規則，在
    `(source_table, target_model, confidence)` 上打平——但只有整數那張表的
    `ModelMapping.reasons` 多附一行「表名已強制轉換」的附註（`str` 那張表沒有
    這行）。

    修正輪次 3 Critical 1 之後，這兩筆 `ModelMapping`（`target_model`／
    `confidence` 一致，只有 `reasons` 不同）不再是「排序鍵要處理的並列」，
    而是在排序之前就被視為同一件事實合併成一筆——`reasons` 取聯集後排序，
    這個合併過程本身就與輸入順序無關，所以兩種輸入順序必須逐位元組相同。
    """
    rules = RuleSet(
        table_rules={"42": TableRule("42", "res.partner", "test#42")},
        column_rules={},
    )
    table_int = {
        "name": 42,
        "columns": [{"name": "cust_no", "type": "CHAR(10)", "nullable": True, "default": None}],
        "primary_key": [],
        "foreign_keys": [],
    }
    table_str = {
        "name": "42",
        "columns": [{"name": "cust_tel", "type": "CHAR(20)", "nullable": True, "default": None}],
        "primary_key": [],
        "foreign_keys": [],
    }

    inventory_forward = _inventory([table_int, table_str])
    inventory_reversed = _inventory([table_str, table_int])

    mapping_forward = build_mapping(
        inventory_forward, CATALOG, rules, event_log=_log(tmp_path / "forward")
    )
    mapping_reversed = build_mapping(
        inventory_reversed, CATALOG, rules, event_log=_log(tmp_path / "reversed")
    )

    # 兩筆本來會在 (source_table, target_model, confidence) 上打平、只有
    # reasons 不同的 ModelMapping，現在合併成一筆——reasons 聯集後有兩行
    # （命中規則本身、加上表名強制轉換附註）。
    assert len(mapping_forward.models) == 1
    merged = mapping_forward.models[0]
    assert merged.source_table == "42"
    assert merged.target_model == "res.partner"
    assert merged.confidence == 100
    assert len(merged.reasons) == 2
    assert mapping_forward.conflicts == []

    assert mapping_forward.to_dict() == mapping_reversed.to_dict()


# ---------------------------------------------------------------------------
# 修正輪次 1 Important 4（人類裁定）：表沒有直接對到模型時，從欄位規則推論
# ---------------------------------------------------------------------------


def test_table_with_no_table_rule_but_unanimous_column_rules_infers_model(tmp_path: Path):
    """`weird_tbl` 表名對 catalog 裡每個模型的 name_score 都是 0（沒有共同
    token），也沒有 table rule；但它的兩個欄位都有 column rule，且都指向
    `res.partner`——人類已經明確教過這件事，不該因為表本身沒有規則就被忽略
    （`rules/README.md:30` 明講 `tables` 是可選的）。應推論表對應到
    `res.partner`，confidence=100，reasons 要講清楚這是推論、不是名稱比對。
    """
    rules = RuleSet(
        table_rules={},
        column_rules={
            ("weird_tbl", "a_col"): ColumnRule(
                source_table="weird_tbl",
                source_column="a_col",
                target_model="res.partner",
                target_field="ref",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#weird_tbl.a_col",
            ),
            ("weird_tbl", "b_col"): ColumnRule(
                source_table="weird_tbl",
                source_column="b_col",
                target_model="res.partner",
                target_field="street",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#weird_tbl.b_col",
            ),
        },
    )
    inventory = _inventory([_table("weird_tbl", [("a_col", "CHAR(10)"), ("b_col", "CHAR(60)")])])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, rules, event_log=log)

    assert len(mapping.models) == 1
    model_mapping = mapping.models[0]
    assert model_mapping.source_table == "weird_tbl"
    assert model_mapping.target_model == "res.partner"
    assert model_mapping.confidence == 100
    assert model_mapping.tier == Tier.AUTO
    # 修正輪次 2 Minor 3：跟它的姊妹矛盾測試
    # （test_table_with_no_table_rule_and_contradicting_column_rules_is_table_conflict）
    # 一樣嚴謹，釘住完整理由字串，而不是只檢查「有沒有出現『推論』兩個字」。
    expected_reason = (
        "資料表 `weird_tbl` 本身沒有對到任何模型（沒有資料表規則，表名與所有 catalog "
        "模型的名稱啟發式評分也都是 0），但其欄位規則（test#weird_tbl.a_col, "
        "test#weird_tbl.b_col）一致指向 `res.partner`，因此從欄位規則推論這張表對應到"
        "該模型——這是推論，不是名稱猜測"
    )
    assert model_mapping.reasons == [expected_reason]

    assert mapping.conflicts == []
    assert len(mapping.mappings) == 2
    assert {fm.target.field for fm in mapping.mappings} == {"ref", "street"}
    assert not log.has_fatal(Stage.MAP)


def test_table_with_no_table_rule_and_contradicting_column_rules_is_table_conflict(
    tmp_path: Path,
):
    """`weird_tbl2` 一樣沒有表名匹配、沒有 table rule，但它的兩個欄位規則
    指向**不同**模型——這是真正的矛盾（人類知識彼此打架），不能任選一邊，
    也不能悄悄丟掉：要產生一筆表級 `MappingConflict`（`kind=TABLE`，
    `candidates` 是排序過的競爭模型），不產生 `ModelMapping`，這張表的所有
    欄位改走既有的「表對不上模型」unmapped 路徑。
    """
    rules = RuleSet(
        table_rules={},
        column_rules={
            ("weird_tbl2", "a_col"): ColumnRule(
                source_table="weird_tbl2",
                source_column="a_col",
                target_model="res.partner",
                target_field="ref",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#weird_tbl2.a_col",
            ),
            ("weird_tbl2", "b_col"): ColumnRule(
                source_table="weird_tbl2",
                source_column="b_col",
                target_model="product.template",
                target_field="name",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#weird_tbl2.b_col",
            ),
        },
    )
    inventory = _inventory([_table("weird_tbl2", [("a_col", "CHAR(10)"), ("b_col", "CHAR(60)")])])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, rules, event_log=log)

    assert mapping.models == []
    assert mapping.mappings == []
    assert len(mapping.conflicts) == 1
    conflict = mapping.conflicts[0]
    assert conflict.kind == ConflictKind.TABLE
    assert conflict.column is None
    assert conflict.table == "weird_tbl2"
    assert conflict.candidates == ["product.template", "res.partner"]

    assert len(mapping.unmapped) == 2
    assert {u.column for u in mapping.unmapped} == {"a_col", "b_col"}
    assert all(
        u.reason
        == "來源表本身對不上任何模型，其欄位規則又指向不同模型，未產生 ModelMapping，欄位無法比對"
        for u in mapping.unmapped
    )
    assert any(e["error_code"] == "E-2001" for e in log.events)
    # E-2001 是致命錯誤——這與其他 conflict 分支一致（見
    # test_column_rule_pointing_to_different_model_than_table_is_conflict_and_fatal），
    # 表級的規則矛盾不該例外。
    assert log.has_fatal(Stage.MAP)


def test_important4_does_not_disturb_builtin_baseline_with_rules(tmp_path: Path):
    """人類裁定第一條 controller-verified baseline：內建 catalog + 內建規則
    跑 case01 必須維持完全不變（cust_mast 有 table rule，新的欄位規則推論
    分支根本不該被觸發）。"""
    inventory = _case01_inventory()
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, RULES, event_log=log)

    assert len(mapping.models) == 1
    assert len(mapping.mappings) == 7
    assert mapping.unmapped == []
    assert mapping.conflicts == []
    assert all(fm.confidence == 100 and fm.tier == Tier.AUTO for fm in mapping.mappings)
    assert mapping.stats.coverage_percent == 100
    assert mapping.stats.auto == 7
    assert not log.events


def test_important4_does_not_disturb_builtin_baseline_with_empty_rules(tmp_path: Path):
    """人類裁定第二條 controller-verified baseline：內建 catalog + 空規則目錄
    跑 case01 必須維持完全不變（沒有任何欄位規則可以推論，落回舊行為）。"""
    inventory = _case01_inventory()
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, EMPTY_RULES, event_log=log)

    assert mapping.models == []
    assert mapping.mappings == []
    assert mapping.conflicts == []
    assert len(mapping.unmapped) == 7
    assert all(u.reason == "來源表找不到對應的 Odoo 模型" for u in mapping.unmapped)
    assert mapping.stats.coverage_percent == 0

    e2002_events = [e for e in log.events if e["error_code"] == "E-2002"]
    assert len(e2002_events) == 1
    assert not log.has_fatal(Stage.MAP)


# ---------------------------------------------------------------------------
# Minor 7：非字串表名保留原始值，不再被硬轉成 ""
# ---------------------------------------------------------------------------


def test_non_string_table_name_is_coerced_with_str_and_reason_notes_it(tmp_path: Path):
    inventory = _inventory(
        [
            {
                "name": 123,
                "columns": [{"name": "col1", "type": "CHAR(1)", "nullable": True, "default": None}],
                "primary_key": [],
                "foreign_keys": [],
            }
        ]
    )
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, EMPTY_RULES, event_log=log)

    # 原始值 123 仍然可以在輸出裡辨識出來（str(123) == "123"），不再是空字串。
    assert len(mapping.unmapped) == 1
    assert mapping.unmapped[0].table == "123"
    assert mapping.unmapped[0].column == "col1"


# ---------------------------------------------------------------------------
# Minor 8：欄位規則指到的目標欄位不存在於本次提供的 catalog
# ---------------------------------------------------------------------------


def test_column_rule_target_field_missing_from_supplied_catalog_is_visible_in_reason(
    tmp_path: Path,
):
    # 用一個「精簡版」catalog（沒有 ref 欄位）搭配指向 res.partner.ref 的規則，
    # 模擬規則檔驗證時用的 catalog 與這次呼叫端傳入的 catalog 不成對的情形。
    slim_catalog = {
        "res.partner": OdooModel(
            name="res.partner",
            description="精簡版，刻意缺少 ref 欄位",
            fields={
                "name": OdooField(
                    name="name", type="char", required=True, module="base", help="名稱"
                )
            },
        )
    }
    rules = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast")},
        column_rules={
            ("cust_mast", "cust_no"): ColumnRule(
                source_table="cust_mast",
                source_column="cust_no",
                target_model="res.partner",
                target_field="ref",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#cust_mast.cust_no",
            )
        },
    )
    inventory = _inventory([_table("cust_mast", [("cust_no", "CHAR(10)")])])

    mapping = build_mapping(inventory, slim_catalog, rules, event_log=_log(tmp_path))

    assert len(mapping.mappings) == 1
    fm = mapping.mappings[0]
    assert fm.confidence == 100
    assert fm.tier == Tier.AUTO
    assert fm.target.type == ""
    assert any("不存在於本次提供的 catalog" in reason for reason in fm.reasons)


# ---------------------------------------------------------------------------
# Minor 9：表的模型是名稱猜出來的時候，欄位的 reasons 要記一筆 provenance
# ---------------------------------------------------------------------------


def test_heuristically_matched_table_notes_the_guess_in_field_reasons(tmp_path: Path):
    # "partner_master" 沒有 table rule，但表名對 res.partner 的最後一段
    # name_score 唯一勝出（見 test_no_table_rule_uses_name_score...）——
    # "partner" 是 "partner_master" 正規化後 token 的子集，name_score 0.8，
    # confidence=80。這是純表名猜測而來的模型，不是規則、也不是從欄位規則
    # 推論。"active" 對 res.partner.active 的 name_score 是 1.0，型別需轉換，
    # 欄位單獨算是 confidence=50。
    inventory = _inventory([_table("partner_master", [("active", "CHAR(1)")])])

    mapping = build_mapping(inventory, CATALOG, EMPTY_RULES, event_log=_log(tmp_path))

    assert len(mapping.models) == 1
    assert mapping.models[0].target_model == "res.partner"
    assert mapping.models[0].confidence == 80

    assert len(mapping.mappings) == 1
    fm = mapping.mappings[0]
    # 修正輪次 3 Important 3（人類裁定）：表的模型是猜的，這張表底下每一筆
    # FieldMapping 的 confidence 都要按比例打折——round(50 × 80 / 100) = 40，
    # 而不是維持欄位單獨算出來的 50。provenance 附註（Minor 9）仍然保留，
    # 只是多附一行算式本身。
    assert fm.confidence == 40
    assert fm.tier == Tier.NEEDS_HUMAN
    assert any("表名啟發式猜測" in reason and "confidence=80" in reason for reason in fm.reasons)
    assert any("round(50" in reason and "= 40" in reason for reason in fm.reasons)


# ---------------------------------------------------------------------------
# Minor 10：MapFailed 缺 synthetic 鍵、to_dict() 對 schema 驗證（含表級衝突）
# ---------------------------------------------------------------------------


def test_inventory_missing_synthetic_key_raises_map_failed(tmp_path: Path):
    inventory = _case01_inventory()
    del inventory["synthetic"]
    log = _log(tmp_path)

    with pytest.raises(MapFailed):
        build_mapping(inventory, CATALOG, RULES, event_log=log)

    assert any(e["error_code"] == "E-2001" for e in log.events)


def test_to_dict_validates_against_schema_for_case01(tmp_path: Path):
    inventory = _case01_inventory()
    mapping = build_mapping(inventory, CATALOG, RULES, event_log=_log(tmp_path))

    _validate_against_schema(mapping.to_dict())


def test_to_dict_validates_against_schema_with_empty_rules(tmp_path: Path):
    inventory = _case01_inventory()
    mapping = build_mapping(inventory, CATALOG, EMPTY_RULES, event_log=_log(tmp_path))

    _validate_against_schema(mapping.to_dict())


def test_to_dict_validates_against_schema_with_table_level_conflict(tmp_path: Path):
    """這是 schema 先前完全沒被設計要表達的形狀——表級衝突（`column: null`）
    ——所以特別獨立驗證一次，而不是只靠上面兩個 corpus 案例（那兩個都沒有
    表級衝突）。"""
    catalog = {
        "a.order": OdooModel(
            name="a.order",
            description="測試用",
            fields={"f": OdooField(name="f", type="char", required=True, module="base", help="h")},
        ),
        "b.order": OdooModel(
            name="b.order",
            description="測試用",
            fields={"f": OdooField(name="f", type="char", required=True, module="base", help="h")},
        ),
    }
    inventory = _inventory([_table("order", [("col1", "CHAR(1)")])])

    mapping = build_mapping(inventory, catalog, EMPTY_RULES, event_log=_log(tmp_path))

    assert len(mapping.conflicts) == 1
    assert mapping.conflicts[0].kind == ConflictKind.TABLE
    payload = mapping.to_dict()
    assert payload["conflicts"][0]["column"] is None
    _validate_against_schema(payload)


# ---------------------------------------------------------------------------
# 修正輪次 3 Critical 1：重複表名彼此矛盾時，不能靜靜輸出互相矛盾的高信度映射
# ---------------------------------------------------------------------------


def test_duplicate_table_definition_with_contradicting_column_types_is_column_conflict(
    tmp_path: Path,
):
    """controller 重現的具體案例：`inventory.schema.json` 的 `tables` 陣列
    沒有 `uniqueItems`，`scan` 也不去重，所以一份把 `cust_mast` 定義兩次、
    型別互相矛盾的來源 schema（第一次 `cust_no CHAR(10)`／`cust_stus
    CHAR(1)`，第二次 `cust_no INTEGER`／`cust_stus DATE`）是完全合法的
    inventory 輸入。內建的 `cust_mast` 欄位規則（`res_partner.yaml`）完全
    不檢查來源型別，兩次出現各自都以 confidence 100／auto 套用同一條規則，
    修正前會讓同一個 `(cust_mast, cust_no)`／`(cust_mast, cust_stus)` 鍵各自
    輸出兩筆內容矛盾的 `FieldMapping`，`conflicts` 卻是空的、覆蓋率虛報成
    100%。修正後：矛盾的兩筆都不能被當成可信映射输出，改記為欄位級衝突，
    絕對底線是不能再回報「0 個衝突」＋「覆蓋率 100%」。
    """
    inventory = _inventory(
        [
            _table("cust_mast", [("cust_no", "CHAR(10)"), ("cust_stus", "CHAR(1)")]),
            _table("cust_mast", [("cust_no", "INTEGER"), ("cust_stus", "DATE")]),
        ]
    )
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, RULES, event_log=log)

    # 絕對底線：不能再回報「0 個衝突」＋「覆蓋率 100%」。
    assert mapping.conflicts != []
    assert mapping.stats.coverage_percent != 100

    conflict_columns = {c.column for c in mapping.conflicts if c.kind == ConflictKind.COLUMN}
    assert conflict_columns == {"cust_no", "cust_stus"}
    assert all(c.kind == ConflictKind.COLUMN for c in mapping.conflicts)

    # 矛盾的兩筆都不能被當成可信的 FieldMapping 靜靜輸出。
    assert mapping.mappings == []
    assert mapping.stats.mapped_columns == 0

    e2001_events = [e for e in log.events if e["error_code"] == "E-2001"]
    assert len(e2001_events) == 2
    assert log.has_fatal(Stage.MAP)

    # ModelMapping 本身沒有矛盾（兩次都命中同一條表規則、判定出同一個模型），
    # 應該被去重成一筆，而不是輸出兩筆鍵完全相同、無法區分彼此的 ModelMapping。
    assert len(mapping.models) == 1
    assert mapping.models[0].source_table == "cust_mast"
    assert mapping.models[0].target_model == "res.partner"

    _validate_against_schema(mapping.to_dict())


def test_duplicate_table_definition_with_byte_identical_columns_is_deduped_not_doubled(
    tmp_path: Path,
):
    """跟上一條互補：如果重複表名的兩次出現對同一個欄位算出完全相同的
    `FieldMapping`（型別也相同），這不是矛盾——去重成一筆，而不是把同一件
    事實原封不動地輸出兩次。"""
    inventory = _inventory(
        [
            _table("cust_mast", [("cust_no", "CHAR(10)")]),
            _table("cust_mast", [("cust_no", "CHAR(10)")]),
        ]
    )
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, RULES, event_log=log)

    assert mapping.conflicts == []
    assert len(mapping.mappings) == 1
    assert mapping.mappings[0].source.column == "cust_no"
    assert mapping.mappings[0].confidence == 100
    assert len(mapping.models) == 1
    assert not log.has_fatal(Stage.MAP)


# ---------------------------------------------------------------------------
# 最終整分支審查修正 Important 1：dedup 必須同時涵蓋 unmapped／conflicts，
# 不能只涵蓋 models／mappings——四個清單對同一份重複輸入必須給出一致的答案
# ---------------------------------------------------------------------------


def test_duplicate_table_with_identical_unmapped_column_is_deduped_honestly(
    tmp_path: Path,
):
    """一張表在 inventory 裡內容逐位元組相同地出現兩次，4 個欄位對得上、
    1 個對不上：`mappings`／`models` 早就靠既有的 `_resolve_duplicate_*`
    收斂成 4 筆／1 筆，但那個對不上的欄位在兩次表項各自獨立產生一筆內容
    完全相同的 `UnmappedColumn`——修正前這兩筆不會被收斂，讓
    `source_columns` 虛報成 6、`coverage_percent` 虛報成 67，甚至可能因此
    觸發一個原本不該存在的 E-2002（67 < 80 門檻）。誠實的答案是 5 個來源
    欄位、80% 覆蓋率、沒有警告。
    """
    mapped_names = ["fld_a", "fld_b", "fld_c", "fld_d"]
    unmapped_names = ["zzz_unmatched"]
    inventory, rules, catalog = _coverage_boundary_inventory_and_rules(mapped_names, unmapped_names)
    table = inventory["tables"][0]
    inventory["tables"] = [table, table]  # 同一張表逐位元組相同地出現兩次
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert len(mapping.mappings) == 4
    assert len(mapping.unmapped) == 1
    assert mapping.unmapped[0].column == "zzz_unmatched"
    assert mapping.stats.source_columns == 5
    assert mapping.stats.coverage_percent == 80
    assert not any(e["error_code"] == "E-2002" for e in log.events)
    assert mapping.conflicts == []


def test_duplicate_table_definition_with_identical_table_level_conflict_is_deduped_to_one(
    tmp_path: Path,
):
    """跟上一條互補：重複的是「表級衝突」而不是「單一欄位對不上」。兩份內容
    逐位元組相同的 `order` 表各自獨立呼叫 `_resolve_target_model`，各自命中
    同一個表名對兩個模型並列的衝突——修正前這會產生兩筆內容完全相同的
    `MappingConflict`／兩筆 E-2001，「2 個衝突」講的其實是同一件事。修正後
    只剩一筆。"""
    catalog = {
        "a.order": OdooModel(
            name="a.order",
            description="測試用",
            fields={"f": OdooField(name="f", type="char", required=True, module="base", help="h")},
        ),
        "b.order": OdooModel(
            name="b.order",
            description="測試用",
            fields={"f": OdooField(name="f", type="char", required=True, module="base", help="h")},
        ),
    }
    table = _table("order", [("col1", "CHAR(1)"), ("col2", "CHAR(1)")])
    inventory = _inventory([table, table])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, EMPTY_RULES, event_log=log)

    assert mapping.models == []
    assert mapping.mappings == []
    assert len(mapping.conflicts) == 1
    assert mapping.conflicts[0].kind == ConflictKind.TABLE
    assert mapping.conflicts[0].candidates == ["a.order", "b.order"]

    assert len(mapping.unmapped) == 2
    assert {u.column for u in mapping.unmapped} == {"col1", "col2"}

    e2001_events = [e for e in log.events if e["error_code"] == "E-2001"]
    assert len(e2001_events) == 1
    assert mapping.stats.source_columns == 2


def test_resolve_duplicate_model_mappings_helper_reports_conflict_for_contradicting_cores():
    """最終整分支審查修正 Minor 7：`_resolve_duplicate_model_mappings` 的
    「核心事實不一致」分支經由 `build_mapping` 這個公開路徑不可能被觸發
    ——`_resolve_target_model` 是 `table_name`／`catalog`／`rules` 的純函式，
    同名表兩次呼叫必然得到相同的 `target_model`／`confidence`。這個分支是
    縱深防禦，用直接呼叫這個私有輔助函式的單元測試涵蓋它，而不是假裝能從
    `build_mapping` 端到端地重現一個實際上不可能發生的輸入。
    """
    contradicting = [
        ModelMapping(
            source_table="dup",
            target_model="res.partner",
            confidence=100,
            tier=Tier.AUTO,
            reasons=["第一次判定"],
        ),
        ModelMapping(
            source_table="dup",
            target_model="product.template",
            confidence=80,
            tier=Tier.SUGGESTED,
            reasons=["第二次判定"],
        ),
    ]
    conflicts: list = []

    resolved = _resolve_duplicate_model_mappings(contradicting, conflicts)

    assert resolved == []
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.table == "dup"
    assert conflict.kind == ConflictKind.TABLE
    assert conflict.column is None
    assert conflict.candidates == ["product.template", "res.partner"]


# ---------------------------------------------------------------------------
# 修正輪次 3 Important 3（人類裁定）：欄位信心要繼承表的不確定性
# ---------------------------------------------------------------------------


def test_heuristically_matched_table_propagates_uncertainty_into_field_confidence(
    tmp_path: Path,
):
    """表的模型如果只是靠表名啟發式猜出來的，這張表底下每一個欄位的信心都
    要按比例打折——confidence 是乘積訊號，欄位對得再準，如果連表本身都只是
    猜的，整體確定性不能停在欄位那一層就當作 100。

    `partner_master` 對 `res.partner` 的表名啟發式信心是 80（見
    `test_no_table_rule_uses_name_score_against_model_last_segment`）。欄位
    `ref`（型別 `CHAR(10)`）對 `res.partner.ref` 名稱與型別都完全對得上，
    單獨算會是 confidence=100／tier=auto；乘上表的信心 80 之後，
    round(100 × 80 / 100) = 80，掉到 tier=suggested——`stats.auto` 不能再把
    它算成可以自動套用的欄位。
    """
    inventory = _inventory([_table("partner_master", [("ref", "CHAR(10)")])])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, EMPTY_RULES, event_log=log)

    assert len(mapping.models) == 1
    assert mapping.models[0].target_model == "res.partner"
    assert mapping.models[0].confidence == 80

    assert len(mapping.mappings) == 1
    fm = mapping.mappings[0]
    assert fm.target.model == "res.partner"
    assert fm.target.field == "ref"
    assert fm.confidence == 80
    assert fm.tier == Tier.SUGGESTED
    assert any("round(100" in reason and "= 80" in reason for reason in fm.reasons)

    assert mapping.stats.auto == 0
    assert mapping.stats.suggested == 1
    assert mapping.stats.needs_human == 0


def test_important3_does_not_disturb_case01_builtin_baseline(tmp_path: Path):
    """人類裁定不能動搖既有的兩個 controller-verified baseline：case01 的
    `cust_mast` 是命中表規則（不是啟發式猜測），`heuristic_confidence` 全程
    是 `None`，傳播邏輯必須是無操作，維持 100／auto 不變。"""
    inventory = _case01_inventory()
    log = _log(tmp_path)

    mapping = build_mapping(inventory, CATALOG, RULES, event_log=log)

    assert len(mapping.mappings) == 7
    assert all(fm.confidence == 100 and fm.tier == Tier.AUTO for fm in mapping.mappings)
    assert mapping.stats.auto == 7
    assert not log.events


# ---------------------------------------------------------------------------
# 修正輪次 3 Important 4：coverage_percent 的分母必須含 column-level conflicts
# ---------------------------------------------------------------------------


def test_coverage_denominator_includes_column_level_conflicts(tmp_path: Path):
    """`_build_stats` 的分母是 `mapped + unmapped + column_level_conflicts`，
    不能只用 `mapped + unmapped`——那會讓有衝突的欄位從分母裡消失，覆蓋率
    虛高（這正是本分支先前已經修過一次的同一種缺陷，這裡把它釘成迴歸測試）。

    一張表、一個成功對應的欄位（`known_col`，命中欄位規則）、一個並列衝突的
    欄位（`no`，與 `no_a`／`no_b` 同分並列）：
    source_columns = 1(mapped) + 0(unmapped) + 1(conflict) = 2，
    coverage_percent = round(100 × 1 / 2) = 50。
    """
    catalog = {
        "x.y": OdooModel(
            name="x.y",
            description="測試用模型",
            fields={
                "known": OdooField(
                    name="known", type="char", required=True, module="base", help="k"
                ),
                "no_a": OdooField(name="no_a", type="char", required=True, module="base", help="a"),
                "no_b": OdooField(
                    name="no_b", type="char", required=False, module="base", help="b"
                ),
            },
        )
    }
    rules = RuleSet(
        table_rules={"mix_tbl": TableRule("mix_tbl", "x.y", "test#mix_tbl")},
        column_rules={
            ("mix_tbl", "known_col"): ColumnRule(
                source_table="mix_tbl",
                source_column="known_col",
                target_model="x.y",
                target_field="known",
                transform=Transform(kind=TransformKind.IDENTITY),
                origin="test#mix_tbl.known_col",
            )
        },
    )
    inventory = _inventory([_table("mix_tbl", [("known_col", "CHAR(1)"), ("no", "CHAR(10)")])])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert len(mapping.mappings) == 1
    assert len(mapping.conflicts) == 1
    assert mapping.conflicts[0].kind == ConflictKind.COLUMN
    assert mapping.unmapped == []

    assert mapping.stats.source_columns == 2
    assert mapping.stats.coverage_percent == 50


# ---------------------------------------------------------------------------
# 修正輪次 3 Important 5：E-2002 門檻在邊界值 80／79 上要精準
# ---------------------------------------------------------------------------


def _coverage_boundary_inventory_and_rules(
    mapped_names: list[str], unmapped_names: list[str]
) -> tuple[dict, RuleSet, dict[str, OdooModel]]:
    """組出 N 個命中欄位規則的「必定對得上」欄位，加上 M 個必定對不上任何
    catalog 欄位的欄位（用不會與任何 `f` 開頭欄位共享 token 的名稱），藉此把
    `coverage_percent` 精準釘在某個邊界值上，而不必依賴任何啟發式評分的
    細節。"""
    fields = {
        name: OdooField(name=name, type="char", required=False, module="base", help="h")
        for name in mapped_names
    }
    catalog = {"res.partner": OdooModel(name="res.partner", description="測試用", fields=fields)}
    rules = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test#cust_mast")},
        column_rules={
            ("cust_mast", name): ColumnRule(
                source_table="cust_mast",
                source_column=name,
                target_model="res.partner",
                target_field=name,
                transform=Transform(kind=TransformKind.IDENTITY),
                origin=f"test#cust_mast.{name}",
            )
            for name in mapped_names
        },
    )
    columns = [(name, "CHAR(1)") for name in mapped_names] + [
        (name, "CHAR(1)") for name in unmapped_names
    ]
    inventory = _inventory([_table("cust_mast", columns)])
    return inventory, rules, catalog


def test_coverage_percent_boundary_at_80_emits_no_warning(tmp_path: Path):
    """4 個欄位對得上、1 個對不上：round(100 × 4 / 5) == 80，剛好等於門檻，
    不該發 E-2002。"""
    mapped_names = ["fld_a", "fld_b", "fld_c", "fld_d"]
    unmapped_names = ["zzz_unmatched"]
    inventory, rules, catalog = _coverage_boundary_inventory_and_rules(mapped_names, unmapped_names)
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert mapping.stats.coverage_percent == 80
    assert len(mapping.mappings) == 4
    assert len(mapping.unmapped) == 1
    assert not any(e["error_code"] == "E-2002" for e in log.events)


def test_coverage_percent_boundary_at_79_emits_warning(tmp_path: Path):
    """11 個欄位對得上、3 個對不上：round(100 × 11 / 14) == 79，剛好低於
    80 的門檻一個百分點，必須發 E-2002。"""
    mapped_names = [f"fld_{i}" for i in range(11)]
    unmapped_names = [f"zzz_unmatched_{i}" for i in range(3)]
    inventory, rules, catalog = _coverage_boundary_inventory_and_rules(mapped_names, unmapped_names)
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert mapping.stats.coverage_percent == 79
    assert len(mapping.mappings) == 11
    assert len(mapping.unmapped) == 3
    e2002_events = [e for e in log.events if e["error_code"] == "E-2002"]
    assert len(e2002_events) == 1


# ---------------------------------------------------------------------------
# 修正輪次 3 Minor 6：排序鍵是全序，欄位輸入順序不影響輸出
# ---------------------------------------------------------------------------


def test_swapping_column_order_within_one_table_produces_byte_identical_output(
    tmp_path: Path,
):
    """兩筆 `UnmappedColumn` 只是輸入時欄位順序不同（同一張表、欄位名稱與
    型別互異），排序鍵必須是全序，輸出才會逐位元組相同——這正是先前
    `unmapped.sort` 若退化成只用 `(u.table,)` 這種非全序鍵，也會通過全部
    測試的那個缺口。"""
    inventory_forward = _inventory(
        [_table("no_such_table", [("col_alpha", "CHAR(1)"), ("col_beta", "CHAR(2)")])]
    )
    inventory_reversed = _inventory(
        [_table("no_such_table", [("col_beta", "CHAR(2)"), ("col_alpha", "CHAR(1)")])]
    )

    mapping_forward = build_mapping(
        inventory_forward, CATALOG, EMPTY_RULES, event_log=_log(tmp_path / "forward")
    )
    mapping_reversed = build_mapping(
        inventory_reversed, CATALOG, EMPTY_RULES, event_log=_log(tmp_path / "reversed")
    )

    assert len(mapping_forward.unmapped) == 2
    assert mapping_forward.to_dict() == mapping_reversed.to_dict()


def test_tied_field_candidates_are_sorted_regardless_of_catalog_definition_order(
    tmp_path: Path,
):
    """Minor 6 的另一半：`best_fields = sorted(...)` 這個 `sorted()` 不能被
    拿掉——拿掉之後，並列候選的順序會依賴 Python dict（`model.fields`）的
    插入順序，而不是內容本身。這裡刻意把兩個並列欄位以「非字母序」
    （`no_b` 定義在 `no_a` 之前）寫進 catalog，讓「候選清單字母序排列」與
    「候選清單照 catalog 定義順序排列」這兩種行為在這個測試裡給出不同答案
    ——只有真的呼叫 `sorted()` 才會通過。
    """
    catalog = {
        "x.y": OdooModel(
            name="x.y",
            description="測試用模型",
            fields={
                "no_b": OdooField(
                    name="no_b", type="char", required=False, module="base", help="b"
                ),
                "no_a": OdooField(name="no_a", type="char", required=True, module="base", help="a"),
            },
        )
    }
    rules = RuleSet(
        table_rules={"tied_tbl": TableRule("tied_tbl", "x.y", "test#tied_tbl")},
        column_rules={},
    )
    inventory = _inventory([_table("tied_tbl", [("no", "CHAR(10)")])])

    mapping = build_mapping(inventory, catalog, rules, event_log=_log(tmp_path))

    assert len(mapping.conflicts) == 1
    assert mapping.conflicts[0].candidates == ["x.y.no_a", "x.y.no_b"]


# ---------------------------------------------------------------------------
# Task 3（修正後）：引擎不再從 DDL 的 `foreign_keys` 自動產生 `reference`
# transform（原自動產生分支已證實是死碼：`typing_rules._with_relational`
# 對 many2one／one2many／many2many 一律把 `type_score` 固定為 `0.0`，
# `confidence = round(100 * name_score * type_score)` 因此恆為 0，
# `best_confidence == 0` 在選出 `field_obj` 之前就早退，自動產生分支永遠
# 走不到）。`foreign_keys` 現在只做兩件事：① 與規則宣告的 `reference`
# 衝突時發 `E-2001`；② 讓沒有規則、最終進 `unmapped` 的外鍵欄位有具體理由。
# ---------------------------------------------------------------------------


def _ord_catalog_and_table_rule() -> tuple[dict[str, OdooModel], RuleSet]:
    """本節共用的最小 catalog／table rule：一個叫 `x.ord` 的測試模型，只有
    一個欄位 `cust_no`（型別 `char`），`ord_mast` 表命中它的 table rule。
    `ord_cust_no` 對 `cust_no` 正規化後 token 是子集關係（`{order, customer,
    number}` ⊇ `{customer, number}`），name_score=0.8；型別皆為 char，
    type_score=1.0；confidence = round(100 * 0.8 * 1.0) = 80。"""
    catalog = {
        "x.ord": OdooModel(
            name="x.ord",
            description="測試用",
            fields={
                "cust_no": OdooField(
                    name="cust_no", type="char", required=False, module="base", help="h"
                )
            },
        )
    }
    rules = RuleSet(
        table_rules={"ord_mast": TableRule("ord_mast", "x.ord", "test#ord_mast")},
        column_rules={},
    )
    return catalog, rules


def test_foreign_key_without_column_rule_lands_in_unmapped_with_specific_reason(tmp_path: Path):
    """審查與控制者複驗證實：引擎永遠無法從 DDL 的 `foreign_keys` 自動產生
    `reference`——`typing_rules._with_relational` 對 many2one 一律把
    `type_score` 固定為 `0.0`，`confidence` 因此恆為 0，在選出 `field_obj`
    之前就早退。控制者實測：來源欄位 `partner_id`、DDL 宣告 FK、目標
    `sale.order.partner_id` 型別確實是 `many2one`，結果仍是
    `mappings: []`、`unmapped: [('partner_id', 'catalog 中找不到型別相容且
    名稱相近的欄位')]`——這條通用理由浪費了 DDL 已知的外鍵資訊。

    這裡用最小案例重現同一個形狀：`x.ord` 模型上有一個叫 `partner_id`、
    型別 `many2one` 的欄位；來源欄位同名 `partner_id`（name_score=1.0），
    但 `type_score(CHAR, many2one)` 固定為 `0.0`，confidence 恆為 0。
    DDL 宣告這個欄位是外鍵、參照 `cust_mast.cust_no`，且沒有規則。

    昴君裁決：這種情況必須進 `unmapped`，理由不可以是通用的「catalog 中
    找不到型別相容且名稱相近的欄位」，必須點名這是 DDL 宣告的外鍵、參照
    哪張表哪個欄位，並指向具體可動手的下一步（`rules/`）。"""
    catalog = {
        "x.ord": OdooModel(
            name="x.ord",
            description="測試用",
            fields={
                "partner_id": OdooField(
                    name="partner_id", type="many2one", required=False, module="base", help="h"
                )
            },
        )
    }
    rules = RuleSet(
        table_rules={"ord_mast": TableRule("ord_mast", "x.ord", "test#ord_mast")},
        column_rules={},
    )
    table = _table(
        "ord_mast",
        [("partner_id", "CHAR(10)")],
        foreign_keys=[
            {"columns": ["partner_id"], "ref_table": "cust_mast", "ref_columns": ["cust_no"]}
        ],
    )
    inventory = _inventory([table])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert mapping.mappings == []
    assert len(mapping.unmapped) == 1
    unmapped_col = mapping.unmapped[0]
    assert unmapped_col.table == "ord_mast"
    assert unmapped_col.column == "partner_id"
    # reason 必須點名被參照的表與欄位，且指向具體可動手的下一步（rules/），
    # 不能是通用的「catalog 中找不到型別相容且名稱相近的欄位」原封不動。
    assert "cust_mast" in unmapped_col.reason
    assert "cust_no" in unmapped_col.reason
    assert "rules" in unmapped_col.reason
    assert unmapped_col.reason != "catalog 中找不到型別相容且名稱相近的欄位"
    assert mapping.conflicts == []
    assert not log.has_fatal(Stage.MAP)


def test_column_rule_reference_takes_priority_when_ddl_has_no_foreign_key(tmp_path: Path):
    """行為契約第 1 條：規則明確宣告了 transform，即使該欄位沒有 DDL 外鍵，
    也用規則的值——這裡順便驗證「規則優先」在完全沒有 DDL FK 時也成立。"""
    catalog, table_rules_holder = _ord_catalog_and_table_rule()
    rules = RuleSet(
        table_rules=table_rules_holder.table_rules,
        column_rules={
            ("ord_mast", "ord_cust_no"): ColumnRule(
                source_table="ord_mast",
                source_column="ord_cust_no",
                target_model="x.ord",
                target_field="cust_no",
                transform=Transform(
                    kind=TransformKind.REFERENCE, ref_table="other_tbl", ref_column="other_col"
                ),
                origin="test#ord_mast.ord_cust_no",
            )
        },
    )
    table = _table("ord_mast", [("ord_cust_no", "CHAR(10)")])  # 無 foreign_keys
    inventory = _inventory([table])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert len(mapping.mappings) == 1
    fm = mapping.mappings[0]
    assert fm.transform.kind is TransformKind.REFERENCE
    assert fm.transform.ref_table == "other_tbl"
    assert fm.transform.ref_column == "other_col"
    assert mapping.conflicts == []
    assert mapping.unmapped == []
    assert not log.has_fatal(Stage.MAP)


def test_column_rule_reference_conflicts_with_ddl_foreign_key_emits_e2001(tmp_path: Path):
    """衝突條件：規則宣告了 `reference`，DDL 也宣告了同一欄位的 FK，但
    `ref_table` 不一致——必須發 `E-2001`，記進 `conflicts`，不能挑一邊猜。"""
    catalog, table_rules_holder = _ord_catalog_and_table_rule()
    rules = RuleSet(
        table_rules=table_rules_holder.table_rules,
        column_rules={
            ("ord_mast", "ord_cust_no"): ColumnRule(
                source_table="ord_mast",
                source_column="ord_cust_no",
                target_model="x.ord",
                target_field="cust_no",
                transform=Transform(
                    kind=TransformKind.REFERENCE, ref_table="a_tbl", ref_column="a_col"
                ),
                origin="test#ord_mast.ord_cust_no",
            )
        },
    )
    table = _table(
        "ord_mast",
        [("ord_cust_no", "CHAR(10)")],
        foreign_keys=[{"columns": ["ord_cust_no"], "ref_table": "b_tbl", "ref_columns": ["b_col"]}],
    )
    inventory = _inventory([table])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert mapping.mappings == []
    assert len(mapping.conflicts) == 1
    conflict = mapping.conflicts[0]
    assert conflict.table == "ord_mast"
    assert conflict.column == "ord_cust_no"
    assert conflict.kind is ConflictKind.COLUMN
    # 訊息要同時點名規則的 origin 與 DDL 的宣告內容。
    assert "test#ord_mast.ord_cust_no" in conflict.reason
    assert "a_tbl" in conflict.reason
    assert "a_col" in conflict.reason
    assert "b_tbl" in conflict.reason
    assert "b_col" in conflict.reason

    e2001_events = [e for e in log.events if e["error_code"] == "E-2001"]
    assert len(e2001_events) == 1
    assert log.has_fatal(Stage.MAP)


def test_multi_column_foreign_key_lands_in_unmapped_not_identity(tmp_path: Path):
    """`columns` 長度 > 1 的 FK，MVP 不自動產生 `reference`——無法用單一欄位
    的值組出被參照記錄的完整 external_id。這兩欄必須進 `unmapped`，附明確
    reason（含「MVP」與「複合」），絕對不能悄悄落回 `identity`：那會把外鍵
    原始碼當成一般值寫進 Odoo 的整數關聯欄位，是安靜的、格式合法但語意完全
    錯誤的輸出。"""
    catalog, table_rules_holder = _ord_catalog_and_table_rule()
    rules = RuleSet(table_rules=table_rules_holder.table_rules, column_rules={})
    table = _table(
        "ord_mast",
        [("ord_cust_no", "CHAR(10)"), ("ord_branch_no", "CHAR(4)")],
        foreign_keys=[
            {
                "columns": ["ord_cust_no", "ord_branch_no"],
                "ref_table": "cust_mast",
                "ref_columns": ["cust_no", "branch_no"],
            }
        ],
    )
    inventory = _inventory([table])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert mapping.mappings == []
    assert {u.column for u in mapping.unmapped} == {"ord_cust_no", "ord_branch_no"}
    assert len(mapping.unmapped) == 2
    for unmapped_col in mapping.unmapped:
        assert "MVP" in unmapped_col.reason
        assert "複合" in unmapped_col.reason
    assert mapping.conflicts == []
    assert not any(
        fm.transform.kind is TransformKind.IDENTITY
        and fm.source.column in {"ord_cust_no", "ord_branch_no"}
        for fm in mapping.mappings
    )


def test_foreign_key_without_column_rule_but_mapped_notes_ddl_declaration_in_reasons(
    tmp_path: Path,
):
    """控制者實測：外鍵欄位通常不會進 `unmapped`，而是被低分對應掉——引擎
    明明知道這欄有 DDL 外鍵宣告，卻沒有把這件事寫進 `reasons`，人在
    `mapping.csv`（`reasons` 的最終去處）只看得到名稱重疊率的計算，完全不
    知道這其實是外鍵、可能需要 reference transform 而不是 identity。

    昴君裁決：只要該來源欄位有 DDL 外鍵宣告，就在 `reasons` 加一條說明——
    不管它最後是 mapped 還是 unmapped；點名被參照的表與欄位，並告訴人怎麼
    動手。這裡用 `_ord_catalog_and_table_rule()`（confidence=80，真的落在
    mappings，不是 unmapped）驗證「mapped」這個分支。"""
    catalog, table_rules_holder = _ord_catalog_and_table_rule()
    rules = RuleSet(table_rules=table_rules_holder.table_rules, column_rules={})
    table = _table(
        "ord_mast",
        [("ord_cust_no", "CHAR(10)")],
        foreign_keys=[
            {"columns": ["ord_cust_no"], "ref_table": "cust_mast", "ref_columns": ["cust_no"]}
        ],
    )
    inventory = _inventory([table])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert mapping.unmapped == []
    assert mapping.conflicts == []
    assert len(mapping.mappings) == 1
    fm = mapping.mappings[0]
    assert fm.source.column == "ord_cust_no"
    # 沒有規則，transform 維持 identity（不再自動產生 reference，見上方
    # 「修正」註解），但 reasons 必須點名這是 DDL 宣告的外鍵，參照哪張表
    # 哪個欄位。
    assert fm.transform.kind is TransformKind.IDENTITY
    assert any("cust_mast" in reason and "cust_no" in reason for reason in fm.reasons)
    assert not log.has_fatal(Stage.MAP)


def test_column_rule_declared_transform_does_not_add_ddl_fk_reason(tmp_path: Path):
    """守住邊界：規則已經明確宣告過 transform 的欄位（走 `_apply_column_rule`），
    人已經教過了，不需要再提示——即使 DDL 也宣告了同一欄位的外鍵、且與規則
    一致（不觸發 E-2001 衝突），`reasons` 也不應該多出「DDL 宣告外鍵」這條
    提示，避免噪音。"""
    catalog, table_rules_holder = _ord_catalog_and_table_rule()
    rules = RuleSet(
        table_rules=table_rules_holder.table_rules,
        column_rules={
            ("ord_mast", "ord_cust_no"): ColumnRule(
                source_table="ord_mast",
                source_column="ord_cust_no",
                target_model="x.ord",
                target_field="cust_no",
                transform=Transform(
                    kind=TransformKind.REFERENCE, ref_table="cust_mast", ref_column="cust_no"
                ),
                origin="test#ord_mast.ord_cust_no",
            )
        },
    )
    table = _table(
        "ord_mast",
        [("ord_cust_no", "CHAR(10)")],
        foreign_keys=[
            {"columns": ["ord_cust_no"], "ref_table": "cust_mast", "ref_columns": ["cust_no"]}
        ],
    )
    inventory = _inventory([table])
    log = _log(tmp_path)

    mapping = build_mapping(inventory, catalog, rules, event_log=log)

    assert mapping.conflicts == []
    assert len(mapping.mappings) == 1
    fm = mapping.mappings[0]
    assert fm.transform.kind is TransformKind.REFERENCE
    assert not any("cust_mast" in reason for reason in fm.reasons)
    assert fm.reasons == ["命中欄位規則：test#ord_mast.ord_cust_no"]
    assert not log.has_fatal(Stage.MAP)
