"""`writer.write_mapping` / `write_mapping_csv` / `validate_mapping` 的測試。

寫檔的規格有兩條主線：

1. `mapping.json` 的寫法必須與 `inventory.json`（`extractor.write_inventory`）
   一致：`ensure_ascii=False`、`indent=2`、結尾換行、確定性；且 ruling 1 要求
   證明「`to_dict()` 回傳的 `StrEnum` 實例，經過真正的 `json.dumps` →
   `json.loads` 之後確實變成一般 `str`」——這是 schema 驗證故事能成立的前提，
   Task 1 的 schema 測試從未真正走過 `json.dumps`。
2. `mapping.csv` 是給人在試算表裡審核用的：欄位順序固定、`unmapped`／
   `conflicts` 也要進去（ruling 3：只放對上的欄位會讓人誤以為覆蓋率 100%），
   且輸出要對排序確定（ruling 4）。
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest
from jsonschema import ValidationError

import erp_migrai.mapping as mapping_package
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
from erp_migrai.mapping.writer import (
    TABLE_CONFLICT_COLUMN_LABEL,
    TARGET_CONFLICT_COLUMN_LABEL,
    MappingSchemaError,
    MappingSerializationError,
    validate_mapping,
    write_mapping,
    write_mapping_csv,
)

SCHEMA_PATH = Path(mapping_package.__file__).parent / "schema" / "mapping.schema.json"

CSV_HEADER = [
    "source_table",
    "source_column",
    "source_type",
    "target_model",
    "target_field",
    "target_type",
    "transform",
    "confidence",
    "tier",
    "reasons",
]


def _sample_mapping() -> Mapping:
    """涵蓋三種 `TransformKind`、三個 `Tier`、兩種 `ConflictKind`（含表級
    `column=None`）的一份 `Mapping`，跟 `tests/mapping/test_models.py` 的
    `_build_full_mapping` 同樣的設計理由：不依賴任何 corpus case 剛好長
    什麼樣子，直接把整份契約釘死。
    """
    mappings = [
        FieldMapping(
            source=SourceRef(table="cust_mast", column="cust_no", type="CHAR(10)"),
            target=TargetRef(model="res.partner", field="ref", type="char"),
            transform=Transform(kind=TransformKind.IDENTITY),
            confidence=100,
            tier=Tier.AUTO,
            reasons=["名稱與型別完全對應"],
        ),
        FieldMapping(
            source=SourceRef(table="cust_mast", column="cust_status", type="CHAR(1)"),
            target=TargetRef(model="res.partner", field="active", type="boolean"),
            transform=Transform(
                kind=TransformKind.VALUE_MAP,
                value_map={"Y": True, "N": False},
                default=True,
            ),
            confidence=80,
            tier=Tier.SUGGESTED,
            reasons=["布林值需要人工確認對照表"],
        ),
        FieldMapping(
            source=SourceRef(table="cust_mast", column="cust_country", type="CHAR(2)"),
            target=TargetRef(model="res.partner", field="country_id", type="many2one"),
            transform=Transform(kind=TransformKind.CONSTANT, value="TW"),
            confidence=40,
            tier=Tier.NEEDS_HUMAN,
            reasons=["來源沒有國別欄位，暫以常數頂替，需人工確認"],
        ),
    ]
    unmapped = [
        UnmappedColumn(
            table="cust_mast",
            column="cust_legacy_flag",
            type="CHAR(1)",
            reason="Odoo 無對應概念",
        )
    ]
    conflicts = [
        MappingConflict(
            table="cust_mast",
            column="cust_no2",
            kind=ConflictKind.COLUMN,
            candidates=["res.partner.ref", "res.partner.barcode"],
            reason="兩個目標欄位候選分數相同",
        ),
        MappingConflict(
            table="order_hdr",
            column=None,
            kind=ConflictKind.TABLE,
            candidates=["purchase.order", "sale.order"],
            reason="表名與兩個模型的最後一段同分並列",
        ),
    ]
    return Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=True,
        stats=MappingStats(
            source_columns=10,
            mapped_columns=3,
            coverage_percent=30,
            auto=1,
            suggested=1,
            needs_human=1,
        ),
        models=[
            ModelMapping(
                source_table="cust_mast",
                target_model="res.partner",
                confidence=90,
                tier=Tier.AUTO,
                reasons=["表名與欄位組成高度符合 res.partner"],
            )
        ],
        mappings=mappings,
        unmapped=unmapped,
        conflicts=conflicts,
    )


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


# ---------------------------------------------------------------------------
# write_mapping：與 write_inventory 同樣的寫檔慣例
# ---------------------------------------------------------------------------


def test_write_mapping_returns_path_to_mapping_json(tmp_path):
    path = write_mapping(_sample_mapping(), tmp_path / "out")
    assert path == tmp_path / "out" / "mapping.json"
    assert path.is_file()


def test_write_mapping_output_is_utf8_unescaped_and_newline_terminated(tmp_path):
    path = write_mapping(_sample_mapping(), tmp_path / "out")
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert not raw.endswith("\n\n")
    assert "\\u" not in raw  # ensure_ascii=False：中文理由直接寫出，不轉義


def test_write_mapping_output_is_indented_with_two_spaces(tmp_path):
    path = write_mapping(_sample_mapping(), tmp_path / "out")
    raw = path.read_text(encoding="utf-8")
    assert raw.splitlines()[0] == "{"
    assert raw.splitlines()[1].startswith("  ")  # indent=2，不是 4 或 tab


def test_write_mapping_is_deterministic_across_two_calls(tmp_path):
    mapping = _sample_mapping()
    first = write_mapping(mapping, tmp_path / "a")
    second = write_mapping(mapping, tmp_path / "b")
    assert first.read_bytes() == second.read_bytes()


def test_write_mapping_creates_out_dir_when_missing(tmp_path):
    out_dir = tmp_path / "does" / "not" / "exist" / "yet"
    assert not out_dir.exists()
    path = write_mapping(_sample_mapping(), out_dir)
    assert path.is_file()


# ---------------------------------------------------------------------------
# ruling 1：to_dict() → json.dumps → json.loads 的 round-trip，證明 StrEnum
# 序列化之後真的變成一般 str，而不是巧合通過驗證。
# ---------------------------------------------------------------------------


def test_round_trip_through_real_json_produces_plain_str_enum_values(tmp_path):
    """Task 1 的 schema 測試只驗證過 `to_dict()` 回傳的 dict（裡面還是活的
    `StrEnum` 實例），從未真正經過 `json.dumps`／`json.loads`。這裡從
    `write_mapping` 寫出的檔案讀回來，確認：

    1. 通過 `mapping.schema.json`（`validate_mapping` 本身也會在寫檔前驗證，
       這裡對讀回來的結果再驗證一次，證明「寫出來的東西」本身合法，不只是
       「寫檔前的那份 dict」合法）。
    2. `tier`／`kind` 這些原本是 `StrEnum` 的欄位，reload 後型別是純 `str`，
       值與原本的字面量相等——`StrEnum` 序列化成它的 value 這件事，在這裡
       第一次被真正證明過。
    """
    mapping = _sample_mapping()
    path = write_mapping(mapping, tmp_path / "out")

    loaded = json.loads(path.read_text(encoding="utf-8"))
    validate_mapping(loaded)

    assert type(loaded["mappings"][0]["tier"]) is str
    assert loaded["mappings"][0]["tier"] == "auto"
    assert type(loaded["mappings"][1]["tier"]) is str
    assert loaded["mappings"][1]["tier"] == "suggested"
    assert type(loaded["mappings"][2]["tier"]) is str
    assert loaded["mappings"][2]["tier"] == "needs_human"

    assert type(loaded["mappings"][1]["transform"]["kind"]) is str
    assert loaded["mappings"][1]["transform"]["kind"] == "value_map"

    assert type(loaded["conflicts"][0]["kind"]) is str
    assert loaded["conflicts"][0]["kind"] == "column"
    assert type(loaded["conflicts"][1]["kind"]) is str
    assert loaded["conflicts"][1]["kind"] == "table"
    assert loaded["conflicts"][1]["column"] is None


# ---------------------------------------------------------------------------
# ruling 2：write_mapping 必須在寫出前驗證 schema，違規時不留下半成品檔案
# ---------------------------------------------------------------------------


def test_validate_mapping_accepts_a_schema_valid_dict():
    validate_mapping(_sample_mapping().to_dict())  # 不拋例外就是通過


def test_validate_mapping_rejects_missing_required_field():
    with pytest.raises(ValidationError):
        validate_mapping({"schema_version": "1.0"})


def test_write_mapping_raises_and_leaves_no_file_when_schema_invalid(tmp_path):
    """建構一個違反 schema 的 `Mapping`（`reasons` 為空，違反
    `$defs/reasons` 的 `minItems: 1`），`write_mapping` 必須在寫檔前就
    拋出，且完全不留下任何檔案或目錄——半成品的 `mapping.json` 比沒有還糟。

    最終整分支審查修正 Important 4：先前這裡讓 `jsonschema.ValidationError`
    原樣往外傳，`cli.py` 攤平巢狀 `try` 之後，這個型別跟「使用者 --inventory
    不合規格」共用同一個 `except`，導致這裡（引擎自己產出的 mapping 有缺陷）
    也被誤報成使用者的輸入問題。現在改包成 `MappingSchemaError`，兩者是不同
    型別，呼叫端才能分開接住、給出正確歸咎對象的訊息。
    """
    invalid = _sample_mapping()
    invalid.mappings[0].reasons = []  # 違反 schema：reasons 不得為空

    out_dir = tmp_path / "out"
    assert not out_dir.exists()

    with pytest.raises(MappingSchemaError) as exc_info:
        write_mapping(invalid, out_dir)

    # 訊息必須明講這是映射結果本身（引擎）的缺陷，而不是使用者輸入的問題
    # ——否則使用者會去檢查一份完全合法的 inventory，白費工夫。
    assert "mapping.schema.json" in str(exc_info.value)
    assert "不是你的" in str(exc_info.value)

    assert not out_dir.exists()
    assert not (out_dir / "mapping.json").exists()


def test_write_mapping_raises_serialization_error_and_leaves_no_file_when_value_not_serializable(
    tmp_path,
):
    """最終整分支審查修正（Important 1c）：`mapping.schema.json` 的
    `transform.value` 刻意允許任意型別（見 schema description），一個
    `datetime.date`（例如某條規則檔的 `constant.value` 沒加引號寫成
    `2024-01-01`，繞過了 `rules_loader._ensure_json_serializable` 這第一道
    防線、或由其他呼叫端直接組出來）會通過 `validate_mapping`，卻讓
    `json.dumps` 拋出 `TypeError`。這裡確認 `write_mapping` 把它轉成清楚的
    `MappingSerializationError`，且——跟 schema 驗證失敗一樣——`out_dir`
    完全不會被建立，不留下「只有 events.jsonl 沒有 mapping.json」的半成品
    目錄。
    """
    invalid = _sample_mapping()
    invalid.mappings[2].transform = Transform(kind=TransformKind.CONSTANT, value=date(2024, 1, 1))

    out_dir = tmp_path / "out"
    assert not out_dir.exists()

    with pytest.raises(MappingSerializationError):
        write_mapping(invalid, out_dir)

    assert not out_dir.exists()
    assert not (out_dir / "mapping.json").exists()


def test_write_mapping_rejects_nan_value_instead_of_writing_invalid_json(tmp_path):
    """最終整分支審查修正 Important 3(a)：`json.dumps` 預設 `allow_nan=True`，
    會把 `float("nan")` 靜靜寫成 `mapping.json` 裡的 `NaN`——這不是合法 JSON
    （RFC 8259），會被 `JSON.parse`、Go 的 `encoding/json` 拒絕，是這份公開
    格式最忌諱的「悄悄產出錯誤結果」。`write_mapping` 現在用 `allow_nan=False`
    呼叫 `json.dumps`，讓這種值變成一個可控的 `MappingSerializationError`，
    而不是一份看起來正常、實際上不合規格的 `mapping.json`。
    """
    invalid = _sample_mapping()
    invalid.mappings[2].transform = Transform(kind=TransformKind.CONSTANT, value=float("nan"))

    out_dir = tmp_path / "out"
    assert not out_dir.exists()

    with pytest.raises(MappingSerializationError):
        write_mapping(invalid, out_dir)

    assert not out_dir.exists()
    assert not (out_dir / "mapping.json").exists()


def test_write_mapping_rejects_circular_reference_instead_of_raising_recursion_traceback(
    tmp_path, monkeypatch
):
    """最終整分支審查修正 Important 3(b)：循環參照（PyYAML anchor 如
    `value: &a [*a]` 可以合法產生）會讓 `json.dumps` 拋 `ValueError`，不是
    `TypeError`——先前這裡只接住 `TypeError`，會讓整個例外原樣逃出，變成
    使用者看到的一長串 traceback。現在同時接住 `(TypeError, ValueError)`。

    這裡用 `monkeypatch` 直接讓 `json.dumps` 拋出 `ValueError`，而不是真的
    組一個 Python 層級自我參照的 `list`／`dict`：`Mapping.to_dict()` 是
    `dataclasses.asdict()`，它對 `list`／`dict` 的遞迴是樸素的生成式（不像
    `copy.deepcopy` 有 memo 表偵測循環），一個真正自我參照的 `list` 會讓
    `to_dict()` 本身就以 `RecursionError` 崩潰，連 `write_mapping` 的
    `try` 區塊都到不了——這與 `writer.py` 想接住的情境不同（真實使用者路徑
    是 `rules_loader._ensure_json_serializable` 直接對**尚未包進 dataclass
    的原始 YAML 值**呼叫 `json.dumps`，那裡不會先經過 `asdict()`，見
    `test_rules_loader.py` 對 YAML anchor 的端到端測試）。這裡只需要驗證
    `write_mapping` 接到 `json.dumps` 拋出的 `ValueError` 時的處理邏輯本身
    是正確的。
    """

    def _dumps_raising_value_error(*args, **kwargs):
        raise ValueError("Circular reference detected")

    monkeypatch.setattr("erp_migrai.mapping.writer.json.dumps", _dumps_raising_value_error)

    out_dir = tmp_path / "out"
    assert not out_dir.exists()

    with pytest.raises(MappingSerializationError):
        write_mapping(_sample_mapping(), out_dir)

    assert not out_dir.exists()
    assert not (out_dir / "mapping.json").exists()


def test_write_mapping_normal_output_unaffected_by_allow_nan_false(tmp_path):
    """`allow_nan=False` 只在真的遇到 NaN／Infinity 時才有差異——一般不含
    這種值的 `Mapping`，輸出必須與加這個參數之前逐位元組相同（golden 語料庫
    也依賴這一點：`corpus/case01_partner/expected/mapping.json` 不能因為這個
    修正而改變）。"""
    mapping = _sample_mapping()
    path = write_mapping(mapping, tmp_path / "out")
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_mapping(payload)  # 仍然合法，且沒有引入任何非預期的變化


# ---------------------------------------------------------------------------
# write_mapping_csv：欄位順序、transform／reasons 編碼規則
# ---------------------------------------------------------------------------


def test_csv_header_row_matches_the_exact_required_order(tmp_path):
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = _read_csv_rows(path)
    assert rows[0] == CSV_HEADER


def test_csv_uses_newline_none_and_quotes_commas_and_semicolons_correctly(tmp_path):
    """理由字串裡若剛好包含逗號，csv 模組要能正確加引號跳脫，讀回來仍是原始
    完整字串、不會被誤判成多一欄。"""
    mapping = _sample_mapping()
    mapping.mappings[0].reasons = ['含有逗號, 引號" 與 ； 全形分號的理由']

    path = write_mapping_csv(mapping, tmp_path / "out")
    rows = _read_csv_rows(path)
    row = next(r for r in rows if r[1] == "cust_no")
    assert row[9] == '含有逗號, 引號" 與 ； 全形分號的理由'


def test_csv_field_mapping_row_content(tmp_path):
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = {r[1]: r for r in _read_csv_rows(path)[1:]}

    row = rows["cust_no"]
    assert row[0] == "cust_mast"  # source_table
    assert row[2] == "CHAR(10)"  # source_type
    assert row[3] == "res.partner"  # target_model
    assert row[4] == "ref"  # target_field
    assert row[5] == "char"  # target_type
    assert row[6] == ""  # transform：identity 留空
    assert row[7] == "100"  # confidence
    assert row[8] == "auto"  # tier
    assert row[9] == "名稱與型別完全對應"  # reasons


def test_csv_identity_transform_column_is_blank(tmp_path):
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = {r[1]: r for r in _read_csv_rows(path)[1:]}
    assert rows["cust_no"][6] == ""


def test_csv_value_map_transform_writes_kind_and_explains_map_in_reasons(tmp_path):
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = {r[1]: r for r in _read_csv_rows(path)[1:]}
    row = rows["cust_status"]
    assert row[6] == "value_map"
    # value_map 的實際對照內容不會出現在 transform 欄位（那裡只放 kind），
    # 而是額外説明在 reasons 裡，讓人不用另外開 mapping.json 就能看懂。
    assert "布林值需要人工確認對照表" in row[9]
    assert "Y" in row[9]
    assert "N" in row[9]


def test_csv_constant_transform_writes_kind(tmp_path):
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = {r[1]: r for r in _read_csv_rows(path)[1:]}
    row = rows["cust_country"]
    assert row[6] == "constant"


def test_csv_constant_transform_value_is_visible_in_reasons(tmp_path):
    """最終整分支審查修正（Important 2）：`transform` 欄位只放得下一個
    `kind` 字面值，`constant` 的實際值先前完全沒有出現在 mapping.csv 裡
    ——而「這個常數究竟是什麼」正是人工審核這筆映射時唯一需要確認的事。
    跟 `value_map` 一樣，把值另外説明在 `reasons`。"""
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = {r[1]: r for r in _read_csv_rows(path)[1:]}
    row = rows["cust_country"]
    assert row[6] == "constant"
    assert "constant" in row[9]
    assert "TW" in row[9]


def test_csv_reference_transform_writes_kind_and_ref_table_column_in_reasons(tmp_path):
    """schema 1.1 新增的 `reference`：`transform` 欄位放 `kind` 的字面值，
    參照的來源表與欄位說明在 `reasons`，讓人在試算表裡一眼看出這一欄是
    外鍵、指到哪裡（陷阱二：不能落進 writer.py 尾端『只剩 CONSTANT』的
    無條件分支，否則會被誤寫成一句假話：`轉換方式 constant，值：null`）。
    """
    mapping = Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=True,
        stats=MappingStats(
            source_columns=1,
            mapped_columns=1,
            coverage_percent=100,
            auto=1,
            suggested=0,
            needs_human=0,
        ),
        mappings=[
            FieldMapping(
                source=SourceRef(table="ord_mast", column="ord_cust_no", type="CHAR(10)"),
                target=TargetRef(model="sale.order", field="partner_id", type="many2one"),
                transform=Transform(
                    kind=TransformKind.REFERENCE,
                    ref_table="cust_mast",
                    ref_column="cust_no",
                ),
                confidence=90,
                tier=Tier.AUTO,
                reasons=["外鍵：參照 cust_mast.cust_no"],
            ),
        ],
    )
    path = write_mapping_csv(mapping, tmp_path / "out")
    rows = {r[1]: r for r in _read_csv_rows(path)[1:]}
    row = rows["ord_cust_no"]
    assert row[6] == "reference"
    assert "reference" in row[9]
    assert "cust_mast.cust_no" in row[9]
    # 不能被誤寫成 constant——這正是陷阱二要守住的事。
    assert "constant" not in row[9]


def test_csv_unmapped_row_has_blank_target_and_transform_and_confidence(tmp_path):
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = {r[1]: r for r in _read_csv_rows(path)[1:]}
    row = rows["cust_legacy_flag"]
    assert row[0] == "cust_mast"
    assert row[2] == "CHAR(1)"
    assert row[3] == ""  # target_model
    assert row[4] == ""  # target_field
    assert row[5] == ""  # target_type
    assert row[6] == ""  # transform
    assert row[7] == ""  # confidence
    assert row[8] == "unmapped"
    assert row[9] == "Odoo 無對應概念"


def test_csv_column_conflict_row_has_blank_target_and_tier_conflict(tmp_path):
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = {r[1]: r for r in _read_csv_rows(path)[1:]}
    row = rows["cust_no2"]
    assert row[0] == "cust_mast"
    assert row[3] == ""
    assert row[4] == ""
    assert row[5] == ""
    assert row[6] == ""
    assert row[7] == ""
    assert row[8] == "conflict"
    assert "兩個目標欄位候選分數相同" in row[9]
    assert "res.partner.ref" in row[9]
    assert "res.partner.barcode" in row[9]


def test_csv_table_level_conflict_row_uses_unambiguous_column_label(tmp_path):
    """`kind == table` 時 `column is None`——ruling 3 要求這一列讀起來要
    對人明確，不能跟「欄位名稱剛好是空字串」的畸形輸入混在一起（那種輸入
    在引擎裡一律進 `unmapped`，不會是 `conflict`，但 CSV 讀者不該需要知道
    這個細節才看得懂）。這裡採用 `(整張表)` 當作 `source_column` 的顯示值，
    與 `engine.py` 既有的 `conflict.column or '(整張表)'` 訊息慣例一致。
    """
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = _read_csv_rows(path)[1:]
    row = next(r for r in rows if r[0] == "order_hdr")

    assert row[1] == TABLE_CONFLICT_COLUMN_LABEL
    assert row[1] == "(整張表)"
    assert row[3] == ""  # target_model：並列的模型不只一個，寫在 reasons
    assert row[8] == "conflict"
    assert "表名與兩個模型的最後一段同分並列" in row[9]
    assert "purchase.order" in row[9]
    assert "sale.order" in row[9]


def test_csv_target_conflict_row_uses_dedicated_column_label(tmp_path):
    """Task 4.5：`kind == target` 時 `column is None`——跟 `table` 一樣不能
    留白，但語意不同（不是整張表對不上模型，而是同一張表裡多個來源欄位搶
    同一個目標欄位），所以要用專屬標籤，不能直接借用
    `TABLE_CONFLICT_COLUMN_LABEL`：那會讓 CSV 讀者誤以為這是表級衝突，
    白白跑去查一個其實對得上模型的表。
    """
    mapping = Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=True,
        stats=MappingStats(
            source_columns=2,
            mapped_columns=2,
            coverage_percent=100,
            auto=1,
            suggested=0,
            needs_human=1,
        ),
        conflicts=[
            MappingConflict(
                table="ord_mast",
                column=None,
                kind=ConflictKind.TARGET,
                candidates=["ord_cust_ref", "ord_no"],
                reason=(
                    "資料表 `ord_mast` 有 2 個來源欄位（ord_cust_ref、ord_no）同時對應到"
                    "目標欄位 `sale.order.client_order_ref`——載入階段會互相覆蓋"
                ),
            ),
        ],
    )
    path = write_mapping_csv(mapping, tmp_path / "out")
    rows = _read_csv_rows(path)[1:]
    row = next(r for r in rows if r[0] == "ord_mast")

    assert row[1] == TARGET_CONFLICT_COLUMN_LABEL
    assert row[1] != TABLE_CONFLICT_COLUMN_LABEL
    assert row[3] == ""  # target_model：多個來源欄位搶同一個目標，寫在 reasons
    assert row[8] == "conflict"
    assert "sale.order.client_order_ref" in row[9]
    assert "ord_cust_ref" in row[9]
    assert "ord_no" in row[9]


# ---------------------------------------------------------------------------
# ruling 3：mapped／unmapped／conflicts 三者合計＝CSV 的每一列，一個不多一個不少
# ---------------------------------------------------------------------------


def test_csv_row_count_and_column_pairs_match_the_union_of_all_three_lists(tmp_path):
    mapping = _sample_mapping()
    path = write_mapping_csv(mapping, tmp_path / "out")
    rows = _read_csv_rows(path)[1:]

    assert len(rows) == len(mapping.mappings) + len(mapping.unmapped) + len(mapping.conflicts)

    produced_pairs = {(r[0], r[1]) for r in rows}
    expected_pairs = (
        {(fm.source.table, fm.source.column) for fm in mapping.mappings}
        | {(u.table, u.column) for u in mapping.unmapped}
        | {
            # 跟 `writer.py` 的 `_conflict_row` 一樣按 `kind` 判斷，而不是另外
            # 自己複製一份「`column is None` 就當表級」的邏輯——schema 只把
            # `column` 定成 `["string", "null"]`，並沒有強制
            # `column is None ⇔ kind == "table"` 這個耦合，那個耦合只在
            # `engine.py` 的建構方式裡成立。這裡改成跟生產程式碼同一個判準，
            # 測到的才是生產程式碼實際依賴的不變量，而不是測試自己另外
            # 假設出來、恰好目前相符的另一個不變量。
            (c.table, c.column if c.kind == ConflictKind.COLUMN else TABLE_CONFLICT_COLUMN_LABEL)
            for c in mapping.conflicts
        }
    )
    assert produced_pairs == expected_pairs
    assert len(produced_pairs) == len(rows)  # 每個鍵恰好出現一次，沒有重複列


# ---------------------------------------------------------------------------
# ruling 4：CSV 列必須確定性排序，不依賴三個清單串接的順序
# ---------------------------------------------------------------------------


def test_csv_rows_are_sorted_by_source_table_then_column_not_by_list_concatenation_order(
    tmp_path,
):
    """`mappings`／`unmapped`／`conflicts` 三個清單分別放進 z/a/m 開頭的表，
    若 CSV 只是照這三個清單串接的順序寫出去，得到的表順序會是
    z, a, m；正確的實作要先合併再排序，得到 a, m, z。
    """
    mapping = Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=True,
        stats=MappingStats(
            source_columns=3,
            mapped_columns=1,
            coverage_percent=33,
            auto=1,
            suggested=0,
            needs_human=0,
        ),
        mappings=[
            FieldMapping(
                source=SourceRef(table="z_table", column="col", type="CHAR(1)"),
                target=TargetRef(model="res.partner", field="ref", type="char"),
                transform=Transform(kind=TransformKind.IDENTITY),
                confidence=100,
                tier=Tier.AUTO,
                reasons=["r"],
            )
        ],
        unmapped=[
            UnmappedColumn(table="a_table", column="col", type="CHAR(1)", reason="沒對到"),
        ],
        conflicts=[
            MappingConflict(
                table="m_table",
                column="col",
                kind=ConflictKind.COLUMN,
                candidates=["x.y.a", "x.y.b"],
                reason="並列",
            )
        ],
    )

    path = write_mapping_csv(mapping, tmp_path / "out")
    rows = _read_csv_rows(path)[1:]
    tables_in_order = [r[0] for r in rows]
    assert tables_in_order == ["a_table", "m_table", "z_table"]


def test_write_mapping_csv_is_byte_identical_across_two_calls(tmp_path):
    mapping = _sample_mapping()
    first = write_mapping_csv(mapping, tmp_path / "a")
    second = write_mapping_csv(mapping, tmp_path / "b")
    assert first.read_bytes() == second.read_bytes()


def test_write_mapping_csv_returns_path_to_mapping_csv(tmp_path):
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    assert path == tmp_path / "out" / "mapping.csv"
    assert path.is_file()


# ---------------------------------------------------------------------------
# Minor 6：CSV 公式注入防護
# ---------------------------------------------------------------------------


def test_csv_formula_like_source_column_is_neutralized_with_leading_quote(tmp_path):
    """`source_column`（第一格之後也可能是任何欄位）若剛好以 `=`／`+`／
    `-`／`@` 開頭，會在 Excel／Numbers 開啟時被當成公式執行——這裡確認會
    被加上前導單引號，讓試算表軟體把它當純文字讀。"""
    mapping = Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=True,
        stats=MappingStats(
            source_columns=1,
            mapped_columns=0,
            coverage_percent=0,
            auto=0,
            suggested=0,
            needs_human=0,
        ),
        unmapped=[
            UnmappedColumn(
                table="=cmd|'/c calc'!A0",
                column="+SUM(A1:A9)",
                type="CHAR(1)",
                reason="-2+3 一個看起來像公式的理由",
            )
        ],
    )
    path = write_mapping_csv(mapping, tmp_path / "out")
    rows = _read_csv_rows(path)[1:]
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "'=cmd|'/c calc'!A0"
    assert row[1] == "'+SUM(A1:A9)"
    assert row[9] == "'-2+3 一個看起來像公式的理由"


def test_csv_ordinary_cell_is_not_modified(tmp_path):
    path = write_mapping_csv(_sample_mapping(), tmp_path / "out")
    rows = {r[1]: r for r in _read_csv_rows(path)[1:]}
    assert rows["cust_no"][0] == "cust_mast"
