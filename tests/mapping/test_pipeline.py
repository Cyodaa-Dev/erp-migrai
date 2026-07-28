"""`mapping.pipeline` 的測試（最終整分支審查修正 Minor 7）。

先前這個模組完全沒有專屬的單元測試，只透過 `tests/test_cli_map.py` 的
CLI 端到端測試間接被涵蓋——具體的缺口：刪掉 `pipeline.py`
`find_unused_column_rules` 裡「conflict 那一半併入 `used_keys`」的那一行，
整個測試套件仍然全綠，而那正是 Important 2 缺陷藏身的那一行。這裡直接對
`find_unused_column_rules`（純函式，不需要真的寫檔）與 `run_map`（管線契約）
補上單元測試，讓這一類「刪掉一行也測不出來」的缺口不會再發生。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from erp_migrai.events import EventLog, Stage
from erp_migrai.mapping import pipeline as pipeline_module
from erp_migrai.mapping.catalog import load_catalog
from erp_migrai.mapping.models import (
    SCHEMA_VERSION,
    ConflictKind,
    FieldMapping,
    Mapping,
    MappingConflict,
    MappingStats,
    ModelMapping,
    SourceRef,
    TargetRef,
    Tier,
    Transform,
    TransformKind,
    UnmappedColumn,
)
from erp_migrai.mapping.pipeline import (
    InventoryReadError,
    InventoryValidationError,
    find_unused_column_rules,
    run_map,
)
from erp_migrai.mapping.rules_loader import ColumnRule, RuleSet, TableRule
from erp_migrai.mapping.writer import MappingSchemaError

CATALOG = load_catalog()

_CORPUS_ROOT = Path(__file__).resolve().parents[2] / "corpus"
CASE01_INVENTORY_PATH = _CORPUS_ROOT / "case01_partner" / "expected" / "inventory.json"


def _log(tmp_path: Path) -> EventLog:
    return EventLog(path=tmp_path / "events.jsonl", run_id="test")


def _empty_stats() -> MappingStats:
    return MappingStats(
        source_columns=0, mapped_columns=0, coverage_percent=100, auto=0, suggested=0, needs_human=0
    )


def _column_rule(table: str, column: str, target_model: str, target_field: str) -> ColumnRule:
    return ColumnRule(
        source_table=table,
        source_column=column,
        target_model=target_model,
        target_field=target_field,
        transform=Transform(kind=TransformKind.IDENTITY),
        origin=f"test.yaml#{table}.{column}",
    )


# ---------------------------------------------------------------------------
# find_unused_column_rules：Important 2——unmapped 也要併入 used_keys
# ---------------------------------------------------------------------------


def test_unused_rule_not_reported_when_its_table_never_resolved_to_a_model():
    """最終整分支審查修正 Important 2 的核心重現：`cust_mast.cust_no`／
    `cust_mast.cust_name` 兩條規則鍵名完全正確，但彼此的 `target` 指向不同
    模型，使得 `cust_mast` 本身對不上任何模型（`engine._process_table` 在
    `target_model is None` 時整批欄位改走 `unmapped`，`_process_column`
    根本不會被呼叫）。若 `find_unused_column_rules` 沒有把 `mapping.unmapped`
    併入 `used_keys`，這兩條拼字完全正確的規則會被誤報成「規則鍵名打錯字」
    ——這正是刪掉 `pipeline.py` 那一行仍然全綠的那個缺口。
    """
    rule_set = RuleSet(
        table_rules={},
        column_rules={
            ("cust_mast", "cust_no"): _column_rule("cust_mast", "cust_no", "res.partner", "ref"),
            ("cust_mast", "cust_name"): _column_rule(
                "cust_mast", "cust_name", "sale.order", "name"
            ),
        },
    )
    mapping = Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=False,
        stats=_empty_stats(),
        models=[],
        mappings=[],
        unmapped=[
            UnmappedColumn(
                table="cust_mast",
                column="cust_no",
                type="CHAR(10)",
                reason="來源表本身對不上任何模型，其欄位規則又指向不同模型，"
                "未產生 ModelMapping，欄位無法比對",
            ),
            UnmappedColumn(
                table="cust_mast",
                column="cust_name",
                type="CHAR(60)",
                reason="來源表本身對不上任何模型，其欄位規則又指向不同模型，"
                "未產生 ModelMapping，欄位無法比對",
            ),
        ],
        conflicts=[
            MappingConflict(
                table="cust_mast",
                column=None,
                kind=ConflictKind.TABLE,
                candidates=["res.partner", "sale.order"],
                reason="欄位規則互相矛盾",
            )
        ],
    )

    assert find_unused_column_rules(rule_set, mapping) == []


def test_unused_rule_with_real_typo_is_still_reported():
    """跟上一條測試對稱的另一個方向：規則鍵名真的打錯字（來源欄位其實是
    `cust_no`，規則卻寫 `cust_nol`），這種鍵不會出現在 `mappings`／
    `unmapped`／`conflicts` 任何一個清單裡——必須仍然被回報，Important 2
    的修正不能矯枉過正變成「什麼都不報」。
    """
    rule_set = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test.yaml#cust_mast")},
        column_rules={
            ("cust_mast", "cust_nol"): _column_rule("cust_mast", "cust_nol", "res.partner", "ref"),
        },
    )
    # 真正的來源欄位是 cust_no，命中規則產生一筆 FieldMapping；cust_nol
    # 這個規則鍵則完全沒有對應的來源欄位，不會出現在 mappings／unmapped
    # ／conflicts 任何一個清單裡。
    mapping = Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=False,
        stats=_empty_stats(),
        models=[
            ModelMapping(
                source_table="cust_mast",
                target_model="res.partner",
                confidence=100,
                tier=Tier.AUTO,
                reasons=["命中資料表規則"],
            )
        ],
        mappings=[
            FieldMapping(
                source=SourceRef(table="cust_mast", column="cust_no", type="CHAR(10)"),
                target=TargetRef(model="res.partner", field="ref", type="char"),
                transform=Transform(kind=TransformKind.IDENTITY),
                confidence=50,
                tier=Tier.NEEDS_HUMAN,
                reasons=["啟發式評分"],
            )
        ],
        unmapped=[],
        conflicts=[],
    )

    assert find_unused_column_rules(rule_set, mapping) == ["test.yaml#cust_mast.cust_nol"]


def test_unused_rule_counted_via_column_conflict_half_of_used_keys():
    """`used_keys` 還有一半是「命中規則、但規則的 target_model 與表已經對到
    的模型不一致」而被記成 `ConflictKind.COLUMN` 的情形（見
    `engine._apply_column_rule`）——這條規則確確實實被用上了（只是結果是
    衝突，不是映射），不該被誤報成沒被用到。這是本檔案要專門守住、避免
    「刪掉這一行也測不出來」的那一半（見模組 docstring）。
    """
    rule_set = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test.yaml#cust_mast")},
        column_rules={
            ("cust_mast", "cust_no"): _column_rule(
                "cust_mast", "cust_no", "product.template", "name"
            ),
        },
    )
    mapping = Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=False,
        stats=_empty_stats(),
        models=[
            ModelMapping(
                source_table="cust_mast",
                target_model="res.partner",
                confidence=100,
                tier=Tier.AUTO,
                reasons=["命中資料表規則"],
            )
        ],
        mappings=[],
        unmapped=[],
        conflicts=[
            MappingConflict(
                table="cust_mast",
                column="cust_no",
                kind=ConflictKind.COLUMN,
                candidates=["product.template.name"],
                reason="規則指到的模型與表已對應的模型不一致",
            )
        ],
    )

    assert find_unused_column_rules(rule_set, mapping) == []


# ---------------------------------------------------------------------------
# Minor 6：規則的來源表若完全不在這份 inventory 裡，不算「值得檢查」的候選
# ---------------------------------------------------------------------------


def test_unused_rule_for_table_absent_from_inventory_is_not_reported():
    """`rules/` 是共用知識庫，會涵蓋愈來愈多資料表；某個 T100 安裝的
    inventory 本來就只含它實際擁有的表。一條規則指向的表完全不在這份
    inventory 裡（不在 `models`／`unmapped`／`conflicts` 任何一筆紀錄裡），
    是預期中的落差，不是規則鍵名打錯字。"""
    rule_set = RuleSet(
        table_rules={},
        column_rules={
            ("some_other_table", "col"): _column_rule(
                "some_other_table", "col", "res.partner", "ref"
            ),
        },
    )
    mapping = Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=False,
        stats=_empty_stats(),
        models=[
            ModelMapping(
                source_table="cust_mast",
                target_model="res.partner",
                confidence=100,
                tier=Tier.AUTO,
                reasons=["命中資料表規則"],
            )
        ],
        mappings=[],
        unmapped=[],
        conflicts=[],
    )

    assert find_unused_column_rules(rule_set, mapping) == []


def test_unused_rule_for_column_in_present_table_is_still_reported():
    """Minor 6 的另一半：表確實存在於這份 inventory 裡，但規則指到的欄位
    鍵沒有任何來源欄位命中——這才是真正「值得檢查」的候選，不能因為
    Minor 6 的過濾矯枉過正變成連這種情形都不報。"""
    rule_set = RuleSet(
        table_rules={"cust_mast": TableRule("cust_mast", "res.partner", "test.yaml#cust_mast")},
        column_rules={
            ("cust_mast", "cust_nonexistent"): _column_rule(
                "cust_mast", "cust_nonexistent", "res.partner", "ref"
            ),
        },
    )
    mapping = Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=False,
        stats=_empty_stats(),
        models=[
            ModelMapping(
                source_table="cust_mast",
                target_model="res.partner",
                confidence=100,
                tier=Tier.AUTO,
                reasons=["命中資料表規則"],
            )
        ],
        mappings=[],
        unmapped=[],
        conflicts=[],
    )

    assert find_unused_column_rules(rule_set, mapping) == ["test.yaml#cust_mast.cust_nonexistent"]


# ---------------------------------------------------------------------------
# run_map：管線契約——讀檔→驗證→載入→build_mapping→寫檔，例外型別各自獨立
# ---------------------------------------------------------------------------


def test_run_map_happy_path_returns_result_with_all_fields(tmp_path: Path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(CASE01_INVENTORY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "out"

    result = run_map(
        inventory_path, out, rules_dir=None, catalog_path=None, event_log=_log(tmp_path)
    )

    assert len(result.mapping.mappings) == 7
    assert result.json_path == out / "mapping.json"
    assert result.csv_path == out / "mapping.csv"
    assert result.json_path.is_file()
    assert result.csv_path.is_file()
    assert result.unused_rule_origins == []
    assert result.rule_set is not None


def test_run_map_missing_inventory_file_raises_inventory_read_error(tmp_path: Path):
    missing = tmp_path / "nope.json"
    out = tmp_path / "out"

    with pytest.raises(InventoryReadError):
        run_map(missing, out, rules_dir=None, catalog_path=None, event_log=_log(tmp_path))


def test_run_map_invalid_json_syntax_raises_json_decode_error(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ 不是合法的 JSON", encoding="utf-8")
    out = tmp_path / "out"

    with pytest.raises(json.JSONDecodeError):
        run_map(bad, out, rules_dir=None, catalog_path=None, event_log=_log(tmp_path))


def test_run_map_inventory_failing_schema_raises_inventory_validation_error_not_bare_jsonschema(
    tmp_path: Path,
):
    """最終整分支審查修正 Important 4：這裡必須是 `InventoryValidationError`
    這個 pipeline 自己的型別，而不是原樣往外傳的 `jsonschema.ValidationError`
    ——否則呼叫端無法把它跟「引擎自己產出的 mapping 不合 schema」分開接住。
    """
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "a valid inventory"}), encoding="utf-8")
    out = tmp_path / "out"

    with pytest.raises(InventoryValidationError) as exc_info:
        run_map(bad, out, rules_dir=None, catalog_path=None, event_log=_log(tmp_path))

    assert str(bad) in str(exc_info.value)
    assert "inventory.schema.json" in str(exc_info.value)


def test_run_map_inventory_validation_error_is_not_a_plain_validation_error(tmp_path: Path):
    """跟上一條測試互補：確認 `InventoryValidationError` 與
    `jsonschema.ValidationError` 是兩個不相干的型別（不是子類別關係），這樣
    `cli.py` 才能安全地只 catch 前者，不會不小心也接住後者、或反過來。"""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "a valid inventory"}), encoding="utf-8")
    out = tmp_path / "out"

    try:
        run_map(bad, out, rules_dir=None, catalog_path=None, event_log=_log(tmp_path))
        pytest.fail("應該要拋出例外")
    except InventoryValidationError as exc:
        assert not isinstance(exc, ValidationError)


def test_run_map_inventory_missing_tables_key_is_caught_by_schema_before_map_failed(
    tmp_path: Path,
):
    """`engine.MapFailed`（inventory 缺少 `tables`／`synthetic` 頂層鍵）在
    `run_map` 這個公開路徑上實際不可達：`inventory.schema.json` 早就把
    `tables`／`synthetic` 列為 required，`validate_inventory`（在
    `build_mapping` 之前執行）會先攔下來，變成 `InventoryValidationError`。
    `MapFailed` 是 `engine.build_mapping` 對呼叫端更寬鬆呼叫方式（略過
    schema 驗證直接組出 inventory 字典，例如 `tests/mapping/test_engine.py`
    直接呼叫 `build_mapping`）的縱深防禦，不是 `run_map` 這條管線本身會
    走到的分支——這裡把這件事明講出來，而不是斷言一個實際上不會發生的
    型別。
    """
    inventory_data = json.loads(CASE01_INVENTORY_PATH.read_text(encoding="utf-8"))
    del inventory_data["tables"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(inventory_data), encoding="utf-8")
    out = tmp_path / "out"

    with pytest.raises(InventoryValidationError) as exc_info:
        run_map(bad, out, rules_dir=None, catalog_path=None, event_log=_log(tmp_path))

    assert "tables" in str(exc_info.value)


def test_run_map_writes_mapping_before_conflicts_make_it_fatal(tmp_path: Path):
    """ruling 3（`pipeline.py` 模組 docstring 明訂的執行順序）：即使
    `mapping.conflicts` 非空、這次執行最終會被判定為致命，`mapping.json`／
    `mapping.csv` 仍然必須先寫出來——呼叫端才能在 exit 1 的同時看到衝突
    內容，而不是必須先解決衝突才看得到任何產出。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "conflicting.yaml").write_text(
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
  cust_mast.cust_name:
    target: sale.order.name
""",
        encoding="utf-8",
    )
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(CASE01_INVENTORY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "out"
    log = _log(tmp_path)

    result = run_map(inventory_path, out, rules_dir=rules_dir, catalog_path=None, event_log=log)

    assert result.mapping.conflicts != []
    assert log.has_fatal(Stage.MAP)
    assert result.json_path.is_file()
    assert result.csv_path.is_file()


def test_run_map_writer_schema_failure_is_mapping_schema_error_not_inventory_problem(
    tmp_path: Path, monkeypatch
):
    """最終整分支審查修正 Important 4 的核心重現：`build_mapping` 若有缺陷、
    算出一個 `confidence=101`（超出 `mapping.schema.json` 允許的 0-100），
    `write_mapping` 內部的 `validate_mapping` 會失敗——這是映射引擎自己的
    缺陷，不是使用者 `--inventory` 的問題。這裡 monkeypatch
    `pipeline.build_mapping` 直接回傳一個違反 schema 的 `Mapping`，確認
    `run_map` 讓 `writer.MappingSchemaError` 原樣往外傳，而不是被誤判成
    `InventoryValidationError`（或原樣的 `jsonschema.ValidationError`）。
    """

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

    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(CASE01_INVENTORY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "out"

    with pytest.raises(MappingSchemaError) as exc_info:
        run_map(inventory_path, out, rules_dir=None, catalog_path=None, event_log=_log(tmp_path))

    assert not isinstance(exc_info.value, InventoryValidationError)
    assert "mapping.schema.json" in str(exc_info.value)
    assert "不是你的" in str(exc_info.value)
