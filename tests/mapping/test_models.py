"""mapping.json 的資料模型與分層邏輯。"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

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
    tier_for,
)

SCHEMA_PATH = Path(mapping_package.__file__).parent / "schema" / "mapping.schema.json"


def test_schema_version_is_1_1():
    assert SCHEMA_VERSION == "1.1"


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (100, Tier.AUTO),
        (90, Tier.AUTO),
        (89, Tier.SUGGESTED),
        (70, Tier.SUGGESTED),
        (69, Tier.NEEDS_HUMAN),
        (0, Tier.NEEDS_HUMAN),
    ],
)
def test_tier_boundaries_follow_the_spec(confidence, expected):
    """三級分層的邊界值必須完全照 spec：>=90 自動、70-89 建議、<70 人工。"""
    assert tier_for(confidence) is expected


def test_tier_rejects_out_of_range_confidence():
    """可信度是 0-100 的整數。超出範圍代表計算有誤，應該大聲失敗。"""
    with pytest.raises(ValueError):
        tier_for(101)
    with pytest.raises(ValueError):
        tier_for(-1)


def test_transform_kind_enum_is_exactly_four_values():
    """對 enum 斷言完整值集合（原則 5.4）。

    只斷言「reference 在裡面」抓不到「有人多加了第五種」；公開契約的
    enum 必須斷言完整集合。
    """
    assert {k.value for k in TransformKind} == {
        "identity",
        "value_map",
        "constant",
        "reference",
    }


def test_reference_transform_carries_ref_table_and_ref_column():
    t = Transform(
        kind=TransformKind.REFERENCE,
        ref_table="cust_mast",
        ref_column="cust_no",
    )
    assert t.ref_table == "cust_mast"
    assert t.ref_column == "cust_no"
    # 其餘三個欄位在 reference 下一律是 None
    assert t.value_map is None
    assert t.default is None
    assert t.value is None


def test_conflict_kinds_are_exactly_two():
    assert sorted(k.value for k in ConflictKind) == ["column", "table"]


def _build_full_mapping() -> Mapping:
    """建構一個用到每個 `$def` 與每個 enum 值的 `Mapping`。

    掃描階段的教訓：schema 突變測試顯示，corpus 沒用到的 `$def` 與 enum 值
    可以被改壞而測試全綠。這個 fixture 資料刻意涵蓋三種 `TransformKind`、
    三個 `Tier`、非空 `unmapped`、非空 `conflicts`、非空 `models`，
    每個 `reasons` 都非空，藉此把整份契約釘死，不依賴任何 corpus case
    剛好長什麼樣。
    """
    field_mappings = [
        FieldMapping(
            source=SourceRef(table="cust_mast", column="cust_no", type="CHAR(10)"),
            target=TargetRef(model="res.partner", field="ref", type="char"),
            transform=Transform(kind=TransformKind.IDENTITY),
            confidence=95,
            tier=Tier.AUTO,
            reasons=["欄位名稱與型別完全對應"],
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
    ]
    return Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=True,
        stats=MappingStats(
            source_columns=10,
            mapped_columns=7,
            coverage_percent=70,
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
        mappings=field_mappings,
        unmapped=[
            UnmappedColumn(
                table="cust_mast",
                column="cust_legacy_flag",
                type="CHAR(1)",
                reason="Odoo 無對應概念",
            )
        ],
        conflicts=[
            MappingConflict(
                table="cust_mast",
                column="cust_no",
                kind=ConflictKind.COLUMN,
                candidates=["res.partner.ref", "res.partner.barcode"],
                reason="兩個目標欄位候選分數相同",
            ),
            # 修正輪次 1（Important 3）：table 級衝突用 `column=None`，`candidates`
            # 是模型名稱清單，不是 `模型.欄位` 字串——與上面的欄位級衝突刻意不同，
            # 讓這個測試同時涵蓋 `ConflictKind` 兩個值。
            MappingConflict(
                table="order_hdr",
                column=None,
                kind=ConflictKind.TABLE,
                candidates=["purchase.order", "sale.order"],
                reason="表名與兩個模型的最後一段同分並列",
            ),
        ],
    )


def test_full_mapping_exercises_every_def_and_enum_value_against_schema():
    """建構一個用到每個 `$def` 與每個 enum 值的 `Mapping`，並驗證它符合 schema。

    修正輪次 3 Critical 2：這裡的原文宣稱「任何一個 `$def` 或 enum 值被改壞，
    這個測試都會抓到」——那是假的。這是一個**只驗證正例**的測試：它只能抓到
    讓「一份本來合法的文件」變得不合法的突變（收緊），完全抓不到**放寬**
    ——例如把某個 `items` 換成 `{}`、把 `additionalProperties` 從 `false`
    改成 `true`、在某個 enum 尾巴多塞一個值、或把 `confidence` 的
    `maximum` 從 100 改成 100000：這些突變不會讓「本來合法」的文件變得不
    合法（不合法的文件本來就沒有義務通過驗證，寬鬆之後同一份文件仍然通過），
    所以這個測試在那五種突變下全部維持綠燈。

    這個測試真正能保證的，只有「這份 fixture 資料本身確實符合目前的
    schema，且確實用到了三種 `TransformKind`、三個 `Tier`、兩種
    `ConflictKind`、非空的 `unmapped`／`conflicts`／`models`，以及處處
    非空的 `reasons`」——也就是**正面覆蓋率**：schema 沒有遺漏任何一個
    `$def` 或 enum 值沒被行為規格用到。抓「放寬」這個方向的責任，落在下面
    `test_schema_rejects_*` 那一整批負例測試上（見 Critical 2 的
    mutation-table 驗證紀錄，附在 `.superpowers/sdd/task-6-report.md`）。
    """
    mapping = _build_full_mapping()
    payload = mapping.to_dict()

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    validator.validate(payload)

    # 再次確認這份資料真的涵蓋了三種 kind 與三個 tier，而不是巧合通過驗證。
    assert {m.transform.kind for m in mapping.mappings} == set(TransformKind)
    all_tiers = {m.tier for m in mapping.mappings} | {m.tier for m in mapping.models}
    assert all_tiers == set(Tier)
    # 同樣涵蓋 ConflictKind 的兩個值：table 級（column 為 None）與欄位級。
    assert {c.kind for c in mapping.conflicts} == set(ConflictKind)
    assert mapping.unmapped
    assert mapping.conflicts
    assert mapping.models
    assert all(fm.reasons for fm in mapping.mappings)
    assert all(mm.reasons for mm in mapping.models)


# ---------------------------------------------------------------------------
# 修正輪次 3 Critical 2：負例 fixture——每個 $def、每個 enum 值、每條
# additionalProperties／required／範圍限制都要有一筆「壞掉的文件」證明
# schema 真的會擋下它。上面的 `test_full_mapping_exercises_every_def_and_
# enum_value_against_schema` 只驗證正例，抓不到「放寬」方向的突變；這裡的
# 每一筆都是嵌進一份完整、其餘部分合法的頂層文件裡再驗證整份文件——
# 唯有如此，才能抓到「某個中繼層級被換成 `{}`」這種放寬（例如
# `properties.models.items` 被換成 `{}`：如果只拿 `model_mapping` 這個
# `$def` 本身去驗證片段，完全看不出頂層的 `items` 已經不再引用它）。
# ---------------------------------------------------------------------------


def _valid_source_ref() -> dict:
    return {"table": "cust_mast", "column": "cust_no", "type": "CHAR(10)"}


def _valid_target_ref() -> dict:
    return {"model": "res.partner", "field": "ref", "type": "char"}


def _valid_transform() -> dict:
    return {
        "kind": "identity",
        "value_map": None,
        "default": None,
        "value": None,
        "ref_table": None,
        "ref_column": None,
    }


def _valid_reference_transform() -> dict:
    return {
        "kind": "reference",
        "value_map": None,
        "default": None,
        "value": None,
        "ref_table": "cust_mast",
        "ref_column": "cust_no",
    }


def _valid_field_mapping() -> dict:
    return {
        "source": _valid_source_ref(),
        "target": _valid_target_ref(),
        "transform": _valid_transform(),
        "confidence": 100,
        "tier": "auto",
        "reasons": ["因為欄位名稱與型別完全對應"],
    }


def _valid_unmapped_column() -> dict:
    return {
        "table": "cust_mast",
        "column": "cust_legacy_flag",
        "type": "CHAR(1)",
        "reason": "Odoo 無對應概念",
    }


def _valid_mapping_conflict() -> dict:
    return {
        "table": "cust_mast",
        "column": "cust_no",
        "kind": "column",
        "candidates": ["res.partner.ref", "res.partner.barcode"],
        "reason": "兩個目標欄位候選分數相同",
    }


def _valid_model_mapping() -> dict:
    return {
        "source_table": "cust_mast",
        "target_model": "res.partner",
        "confidence": 90,
        "tier": "auto",
        "reasons": ["表名與欄位組成高度符合 res.partner"],
    }


def _valid_mapping_stats() -> dict:
    return {
        "source_columns": 10,
        "mapped_columns": 7,
        "coverage_percent": 70,
        "auto": 1,
        "suggested": 1,
        "needs_human": 1,
    }


_BUILDERS = {
    "source_ref": _valid_source_ref,
    "target_ref": _valid_target_ref,
    "transform": _valid_transform,
    "field_mapping": _valid_field_mapping,
    "unmapped_column": _valid_unmapped_column,
    "mapping_conflict": _valid_mapping_conflict,
    "model_mapping": _valid_model_mapping,
    "mapping_stats": _valid_mapping_stats,
}

_REQUIRED_KEYS = {
    "source_ref": ["table", "column", "type"],
    "target_ref": ["model", "field", "type"],
    "transform": ["kind", "value_map", "default", "value", "ref_table", "ref_column"],
    "field_mapping": ["source", "target", "transform", "confidence", "tier", "reasons"],
    "unmapped_column": ["table", "column", "type", "reason"],
    "mapping_conflict": ["table", "column", "kind", "candidates", "reason"],
    "model_mapping": ["source_table", "target_model", "confidence", "tier", "reasons"],
    "mapping_stats": [
        "source_columns",
        "mapped_columns",
        "coverage_percent",
        "auto",
        "suggested",
        "needs_human",
    ],
}

_TOP_LEVEL_REQUIRED = [
    "schema_version",
    "inventory_synthetic",
    "models",
    "mappings",
    "unmapped",
    "conflicts",
    "stats",
]


def _valid_mapping_document() -> dict:
    return {
        "schema_version": "1.0",
        "inventory_synthetic": True,
        "models": [_valid_model_mapping()],
        "mappings": [_valid_field_mapping()],
        "unmapped": [_valid_unmapped_column()],
        "conflicts": [_valid_mapping_conflict()],
        "stats": _valid_mapping_stats(),
    }


def _embed(def_name: str, instance: dict) -> dict:
    """把某個 `$def` 的（可能已經改壞的）實例，嵌進一份其餘部分合法的頂層
    `mapping.json` 文件裡，回傳整份文件——驗證一定要對整份文件做，理由見
    本節開頭的說明。"""
    doc = _valid_mapping_document()
    if def_name == "model_mapping":
        doc["models"] = [instance]
    elif def_name == "field_mapping":
        doc["mappings"] = [instance]
    elif def_name == "unmapped_column":
        doc["unmapped"] = [instance]
    elif def_name == "mapping_conflict":
        doc["conflicts"] = [instance]
    elif def_name == "mapping_stats":
        doc["stats"] = instance
    elif def_name == "source_ref":
        fm = _valid_field_mapping()
        fm["source"] = instance
        doc["mappings"] = [fm]
    elif def_name == "target_ref":
        fm = _valid_field_mapping()
        fm["target"] = instance
        doc["mappings"] = [fm]
    elif def_name == "transform":
        fm = _valid_field_mapping()
        fm["transform"] = instance
        doc["mappings"] = [fm]
    else:
        raise ValueError(f"未知的 def_name：{def_name}")
    return doc


def _assert_document_invalid(doc: dict) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(doc)


def test_valid_mapping_document_fixture_passes_schema():
    """先確認負例測試共用的合法基準文件本身真的合法——否則下面所有
    「應該被拒絕」的測試都可能因為基準本身就壞掉而通過，變成假陽性。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(_valid_mapping_document())


@pytest.mark.parametrize("def_name", list(_BUILDERS))
def test_valid_instance_of_each_def_embeds_and_validates(def_name):
    """同上，但針對每個巢狀 `$def`：把它的合法實例嵌進整份文件，也必須通過
    ——確保下面每一組「移除／添加」的變異，起點確實是一份合法文件。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    doc = _embed(def_name, _BUILDERS[def_name]())
    Draft202012Validator(schema).validate(doc)


@pytest.mark.parametrize(
    ("def_name", "missing_key"),
    [(name, key) for name, keys in _REQUIRED_KEYS.items() for key in keys],
)
def test_missing_required_key_per_def_is_rejected(def_name, missing_key):
    """每個 `$def` 的每一個 `required` 鍵，缺一個就要被拒絕。"""
    instance = _BUILDERS[def_name]()
    del instance[missing_key]
    _assert_document_invalid(_embed(def_name, instance))


@pytest.mark.parametrize("missing_key", _TOP_LEVEL_REQUIRED)
def test_top_level_missing_required_key_is_rejected(missing_key):
    doc = _valid_mapping_document()
    del doc[missing_key]
    _assert_document_invalid(doc)


@pytest.mark.parametrize("def_name", list(_BUILDERS))
def test_unknown_property_on_each_def_is_rejected(def_name):
    """每個宣告 `additionalProperties: false` 的物件型別，多一個未知欄位
    就要被拒絕。"""
    instance = _BUILDERS[def_name]()
    instance["unexpected_extra_field"] = True
    _assert_document_invalid(_embed(def_name, instance))


def test_top_level_unknown_property_is_rejected():
    """頂層文件同樣宣告 `additionalProperties: false`——這正是 controller
    驗證過會被「additionalProperties: false → true」這個突變放過的那個測試。
    """
    doc = _valid_mapping_document()
    doc["unexpected_extra_field"] = True
    _assert_document_invalid(doc)


@pytest.mark.parametrize("bad_confidence", [-1, 101])
def test_field_mapping_confidence_out_of_range_is_rejected(bad_confidence):
    """這正是 controller 驗證過會被「`$defs.confidence.maximum` 100 →
    100000」這個突變放過的那個測試（101 落在放寬後的上限內，突變後就不再
    被拒絕）。"""
    fm = _valid_field_mapping()
    fm["confidence"] = bad_confidence
    _assert_document_invalid(_embed("field_mapping", fm))


@pytest.mark.parametrize("bad_confidence", [-1, 101])
def test_model_mapping_confidence_out_of_range_is_rejected(bad_confidence):
    mm = _valid_model_mapping()
    mm["confidence"] = bad_confidence
    _assert_document_invalid(_embed("model_mapping", mm))


@pytest.mark.parametrize("bad_coverage", [-1, 101])
def test_mapping_stats_coverage_percent_out_of_range_is_rejected(bad_coverage):
    """`coverage_percent` 沒有獨立的 `$def`，只在 `mapping_stats` 內就地
    宣告上下限，這正是 controller 驗證過會被「`properties.stats` → `{}`」
    這個突變放過的那個測試之一。"""
    stats = _valid_mapping_stats()
    stats["coverage_percent"] = bad_coverage
    _assert_document_invalid(_embed("mapping_stats", stats))


def test_field_mapping_empty_reasons_is_rejected():
    fm = _valid_field_mapping()
    fm["reasons"] = []
    _assert_document_invalid(_embed("field_mapping", fm))


def test_model_mapping_empty_reasons_is_rejected():
    mm = _valid_model_mapping()
    mm["reasons"] = []
    _assert_document_invalid(_embed("model_mapping", mm))


def test_field_mapping_bogus_tier_value_is_rejected():
    """這正是 controller 驗證過會被「在 `tier` enum 尾巴多塞一個值」放過的
    那個測試——驗證時把加進 enum 的值換成跟這裡相同的字串，才能具體演示
    RED，見 task-6-report.md 的 mutation table。"""
    fm = _valid_field_mapping()
    fm["tier"] = "bogus_tier_value"
    _assert_document_invalid(_embed("field_mapping", fm))


def test_model_mapping_bogus_tier_value_is_rejected():
    mm = _valid_model_mapping()
    mm["tier"] = "bogus_tier_value"
    _assert_document_invalid(_embed("model_mapping", mm))


def test_mapping_conflict_bogus_kind_value_is_rejected():
    conflict = _valid_mapping_conflict()
    conflict["kind"] = "bogus_kind_value"
    _assert_document_invalid(_embed("mapping_conflict", conflict))


def test_transform_bogus_kind_value_is_rejected():
    transform = _valid_transform()
    transform["kind"] = "bogus_transform_kind"
    _assert_document_invalid(_embed("transform", transform))


# ---------------------------------------------------------------------------
# schema 1.1：`reference` transform（`ref_table`／`ref_column`）
# ---------------------------------------------------------------------------


def test_valid_reference_transform_embeds_and_validates():
    """正向測試：合法的 reference transform 通過驗證。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    doc = _embed("transform", _valid_reference_transform())
    Draft202012Validator(schema).validate(doc)


def test_transform_kind_reference_typo_is_rejected():
    """打錯字的 kind（`refrence`）必須不通過——這不是既有的
    `bogus_transform_kind` 負例的重複：它專門守住『reference 這個具體字串
    真的在 enum 裡，而不是隨便一個字串都被擋下』這件事。"""
    transform = _valid_reference_transform()
    transform["kind"] = "refrence"
    _assert_document_invalid(_embed("transform", transform))


def test_reference_transform_missing_ref_table_key_is_rejected():
    """`transform` 少了 `ref_table` 鍵必須不通過——`required` 已經涵蓋新
    欄位，不能因為新增而悄悄放寬成非必填。"""
    transform = _valid_reference_transform()
    del transform["ref_table"]
    _assert_document_invalid(_embed("transform", transform))


def test_reference_transform_missing_ref_column_key_is_rejected():
    transform = _valid_reference_transform()
    del transform["ref_column"]
    _assert_document_invalid(_embed("transform", transform))


def test_reference_transform_ref_table_wrong_type_is_rejected():
    """`ref_table` 為數字必須不通過——型別宣告是 `["string", "null"]`，
    不是任意型別。"""
    transform = _valid_reference_transform()
    transform["ref_table"] = 12345
    _assert_document_invalid(_embed("transform", transform))


def test_reference_transform_ref_column_wrong_type_is_rejected():
    transform = _valid_reference_transform()
    transform["ref_column"] = 12345
    _assert_document_invalid(_embed("transform", transform))


def test_mapping_conflict_wrong_typed_column_is_rejected():
    """`column` 只允許 `string` 或 `null`，一個整數必須被拒絕。"""
    conflict = _valid_mapping_conflict()
    conflict["column"] = 123
    _assert_document_invalid(_embed("mapping_conflict", conflict))


@pytest.mark.parametrize("bad_version", ["1", "1.0.0", "v1.0", "1.a", "", "1."])
def test_malformed_schema_version_is_rejected(bad_version):
    doc = _valid_mapping_document()
    doc["schema_version"] = bad_version
    _assert_document_invalid(doc)


# ---------------------------------------------------------------------------
# 最終整分支審查修正 Important 5：釘死三個 enum 的**精確**值集合。
#
# 上面所有 `test_*_bogus_*_value_is_rejected` 測試只能證明「某個特定的壞值
# 會被拒絕」——這種負例測試天生無法擋住「在 enum 尾巴多塞一個新值」這種
# 放寬：多塞的那個值從來沒被任何一條負例測試點過名，測試當然不會知道它
# 現在被允許了。controller 驗證過：對 `$defs.tier.enum` 或
# `$defs.transform.properties.kind.enum` 各自追加一個新字串，全部既有測試
# （含上面的負例）仍然全線通過。唯一能擋住「追加」這個方向的手法，是直接
# 讀 schema 本身、斷言 enum 的**精確值集合**（而不是「至少包含」）——追加
# 或刪除任何一個值都會讓 `==` 失敗。
# ---------------------------------------------------------------------------


def test_tier_enum_is_exactly_the_three_defined_values():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert set(schema["$defs"]["tier"]["enum"]) == {"auto", "suggested", "needs_human"}


def test_conflict_kind_enum_is_exactly_the_two_defined_values():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert set(schema["$defs"]["conflict_kind"]["enum"]) == {"table", "column"}


def test_transform_kind_enum_is_exactly_the_four_defined_values():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert set(schema["$defs"]["transform"]["properties"]["kind"]["enum"]) == {
        "identity",
        "value_map",
        "constant",
        "reference",
    }
