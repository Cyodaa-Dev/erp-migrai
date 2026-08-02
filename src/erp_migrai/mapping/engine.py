"""映射引擎：把 `scan` 階段的 `inventory.json` 轉成 `mapping.json`（`Mapping`）。

這是 `map` 階段的核心：對 inventory 裡的每一張表、每一個欄位，依序嘗試

1. 命中 Task 3 的規則檔（人類明確教過的事實）→ 可信度 100、`auto`。
2. 沒有規則 → 用 `naming.name_score` 與 `typing_rules.type_score` 兩個
   啟發式訊號相乘算出可信度，分數 > 0 且唯一最高分才產生對應。
3. 兩者都對不上、或啟發式分數並列 → 進 `unmapped` 或 `conflicts`，絕不
   讓一個來源欄位在三個輸出清單（`mappings`／`unmapped`／`conflicts`）之外
   憑空消失——那正是本專案存在的理由（悄悄產生錯誤或不完整的結果）。

`build_mapping` 的契約是「絕不拋出」，唯一的例外是 `inventory` 本身缺少
本引擎需要的頂層鍵時拋 `MapFailed`（呼叫端已經用 `inventory.schema.json`
驗證過完整結構；這裡只再做一次最小限度的防禦，避免對明顯不成形的輸入
繼續往下跑而產生沒有意義的結果）。任何其他畸形輸入——欄位缺型別、資料表
沒有欄位、重複的表名、非 ASCII 名稱——都必須被安全地吸收，而不是讓整個
`map` 階段中斷。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from erp_migrai.events import E_2001, E_2002, EventLog
from erp_migrai.mapping.catalog import OdooModel
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
from erp_migrai.mapping.naming import name_score
from erp_migrai.mapping.rules_loader import ColumnRule, RuleSet, TableRule
from erp_migrai.mapping.typing_rules import type_score

# 引擎實際會讀取的 inventory 頂層鍵。呼叫端已經用完整的 inventory.schema.json
# 驗證過，這裡只再檢查本引擎真的要用到的兩個鍵——`tables`（逐表逐欄位處理的
# 對象）與 `synthetic`（要原樣傳到 Mapping.inventory_synthetic，不能中途消失）。
# 少了任何一個，後續處理沒有意義，寧可大聲拒絕也不要悄悄產出一份殘缺的結果。
_REQUIRED_INVENTORY_KEYS = ("tables", "synthetic")

# 表→模型或欄位→欄位完全對不上時的固定理由文字（spec 明文要求的措辭）。
_NO_MODEL_MATCH_REASON = "來源表找不到對應的 Odoo 模型"
_MODEL_TIE_UNMAPPED_REASON = "來源表對應的 Odoo 模型評分並列，未產生 ModelMapping，欄位無法比對"
_COLUMN_RULES_CONTRADICT_REASON = (
    "來源表本身對不上任何模型，其欄位規則又指向不同模型，未產生 ModelMapping，欄位無法比對"
)
_NO_FIELD_MATCH_REASON = "catalog 中找不到型別相容且名稱相近的欄位"
# 修正輪次 1 Critical 1：欄位本身在 inventory 裡確實存在，只是它的 `name`
# 缺失或不是字串——`inventory.schema.json` 的 `$defs/column.name` 只要求
# `string`，沒有 `minLength`，所以 `name: ""` 是 schema-valid 輸入。這種
# 欄位不能被悄悄丟棄（那正是本專案存在的理由），但也沒有任何合法規則或
# catalog 欄位可以比對它，所以直接進 unmapped，理由明講缺陷本身。
_MALFORMED_COLUMN_NAME_REASON = "欄位名稱缺失或不是字串，無法比對"

# Task 3：多欄（複合）外鍵所屬的欄位。MVP 版本不自動產生 reference transform
# ——無法只靠單一欄位的值組出被參照記錄完整的 external_id。勉強套用會把外鍵
# 原始碼（例如 `'C001'`）當成一般值寫進 Odoo 的整數關聯欄位，是安靜的、格式
# 合法但語意完全錯誤的輸出，本專案第一原則明確禁止這種行為，所以這種欄位
# 一律進 unmapped，不悄悄落回 identity。
_MULTI_COLUMN_FK_UNSUPPORTED_REASON = (
    "此欄位屬於一個多欄（複合）外鍵，MVP 版本尚不支援自動產生 reference "
    "transform（無法只靠單一欄位的值組出被參照記錄完整的 external_id）；"
    "如需比對，請在規則檔中手動指定這個欄位的 transform"
)


# 修正（審查與控制者複驗定案）：Task 3 原本讓引擎從 DDL 的 `foreign_keys`
# 自動產生 `reference` transform，但這條分支證實是永遠打不開的死碼——
# `typing_rules._with_relational` 對 many2one／one2many／many2many 三種
# 關聯型別，不論來源型別家族，一律把 `type_score` 固定為 `0.0`（計劃 2 的
# 既有設計，理由是 map 階段解析不了 Odoo id，這個設計本身沒有錯）。
# `_process_column` 的 `confidence = round(100 * name_score * type_score)`
# 因此對任何 many2one 目標欄位恆為 `0`，而 `best_confidence == 0` 會在選出
# `field_obj` 之前就早退，走 `_NO_FIELD_MATCH_REASON`。所以「自動產生
# reference」這條分支在真實情境（目標欄位是 many2one）下永遠走不到；就算
# 真的走到（例如測試刻意把目標欄位型別設成非 many2one 的 char），產生的
# `reference` 也是語意錯誤的——把外鍵值硬塞進一個非關聯欄位。靠名稱相似度
# 自動配對外鍵本來就不可靠（真實鼎新 T100 的欄位叫 `ord_cust_no`，Odoo 叫
# `partner_id`，名稱毫無關係），這條路不值得留，已整段拿掉。
#
# DDL 的 `foreign_keys` 資訊仍然保留兩個用途：① `_apply_column_rule` 裡
# 規則宣告 `reference` 與 DDL 外鍵不一致時發 `E-2001`（不變）；②下面
# `_fk_declared_no_match_reason`：讓沒有規則、最終進 `unmapped` 的外鍵欄位
# 有具體理由，而不是浪費 DDL 已知的資訊、丟出一句通用的「找不到型別相容且
# 名稱相近的欄位」。
def _fk_declared_no_match_reason(ref_table: str, ref_column: str) -> str:
    return (
        f"{_NO_FIELD_MATCH_REASON}；但這是 DDL 宣告的外鍵，參照 "
        f"`{ref_table}.{ref_column}`，若這是有效的關聯，請在 rules/ 中明確指定"
        "這個欄位的 reference transform"
    )


# 控制者實測發現：外鍵欄位通常不會進 unmapped，而是被名稱／型別啟發式的低分
# 對應掉（例如 `ord_cust_no` 對到 `sale.order.client_order_ref`，confidence
# 只有 12，但確實 > 0，成功產生一筆 mappings）。這種情況下，上面
# `_fk_declared_no_match_reason` 完全不會被用到——欄位根本沒有進 unmapped。
# 但引擎明明知道這欄有 DDL 外鍵宣告，`reasons`（`mapping.csv` 人工審核時
# 看的欄位）卻只寫了名稱重疊率的計算過程，完全沒有提到「這其實是外鍵」。
# 昴君裁決：只要該來源欄位有 DDL 外鍵宣告，就在 `reasons` 加這條說明——
# 不管它最後是 mapped 還是 unmapped。這裡是「mapped」情境專用的措辭，跟
# `_fk_declared_no_match_reason`（unmapped 情境）刻意不同一句話，避免同一個
# 事實在同一次呼叫（不同分支）裡看起來像是複製貼上兩次。
def _fk_declared_reason_for_mapped_field(ref_table: str, ref_column: str) -> str:
    return (
        f"來源 DDL 宣告此欄位為外鍵，參照 `{ref_table}.{ref_column}`——若這是有效的關聯，"
        "請在 rules/ 中為它指定 reference transform"
    )


# 覆蓋率低於此門檻（百分比）就發 E-2002；spec §「覆蓋率」明訂為 80。
_COVERAGE_WARN_THRESHOLD = 80

# 修正輪次 3 Critical 1（controller 重現）：`inventory.schema.json` 的
# `tables` 陣列沒有 `uniqueItems`，`scan` 也不去重，所以同一個表名在
# inventory 裡出現兩次（甚至型別互相矛盾）是完全 schema-valid 的輸入。
# 這兩個常數是重複表名彼此矛盾時，欄位級／表級衝突各自的固定理由模板。


class MapFailed(Exception):
    """`inventory` 缺少本引擎需要的必要鍵，無法進行映射。"""


def build_mapping(
    inventory: dict[str, Any],
    catalog: dict[str, OdooModel],
    rules: RuleSet,
    *,
    event_log: EventLog,
) -> Mapping:
    """把已驗證過的 `inventory` 字典轉成 `Mapping`。

    對任何輸入都不拋例外，除了 `inventory` 本身不是 dict 或缺少
    `_REQUIRED_INVENTORY_KEYS` 列出的鍵——那種情況下拋 `MapFailed` 並先發一個
    `E-2001` 事件。
    """
    _validate_inventory(inventory, event_log)

    tables_raw = inventory.get("tables")
    tables = tables_raw if isinstance(tables_raw, list) else []
    inventory_synthetic = bool(inventory.get("synthetic"))

    models: list[ModelMapping] = []
    mappings: list[FieldMapping] = []
    unmapped: list[UnmappedColumn] = []
    conflicts: list[MappingConflict] = []

    for table_entry in tables:
        _process_table(table_entry, catalog, rules, models, mappings, unmapped, conflicts)

    # 修正輪次 3 Critical 1：重複表名可能讓同一個 `source_table`／
    # `(table, column)` 鍵產生一筆以上的 `ModelMapping`／`FieldMapping`。
    # 在排序之前先解決——內容完全相同的去重成一筆，內容矛盾的整批拉出來
    # 改記成衝突，不能讓自相矛盾的紀錄以「confidence 100／conflicts 為空」
    # 的樣子流出去。
    models = _resolve_duplicate_model_mappings(models, conflicts)
    mappings = _resolve_duplicate_field_mappings(mappings, conflicts)

    # Task 4.5（昴君裁決）：corpus case03 揭露的缺口——`rules_loader` 的衝突
    # 檢查只擋「同一個來源欄位對到兩個不同的 target」，沒有任何地方檢查
    # 「同一個 target 被兩個不同的來源欄位對應」。到了載入階段，同一筆記錄的
    # 同一個欄位會被後寫的覆蓋，而使用者無從得知哪一個贏。這裡在 `mappings`
    # 最終定案（重複表名已經解決過）之後、去重與排序之前偵測這個形狀，記進
    # `conflicts`——比到載入階段才發現資料被覆蓋便宜太多。
    _detect_target_field_conflicts(mappings, conflicts)

    # 最終整分支審查修正（第三組）Important 1：上面兩個 `_resolve_duplicate_*`
    # 只處理了 `models`／`mappings` 兩個清單——重複表名（`inventory.schema.json`
    # 的 `tables` 陣列沒有 `uniqueItems`）如果是「內容完全相同」的重複，同一張
    # 表的兩次出現各自獨立呼叫 `_process_table`／`_process_column`，會在
    # `unmapped`（例如同一個對不上任何 catalog 欄位的欄位）與 `conflicts`
    # （例如同一個表名對到多個模型並列的表級衝突、或欄位並列衝突）各自產生
    # 內容完全相同的兩筆紀錄——`models`／`mappings` 已經去重、`unmapped`／
    # `conflicts` 卻沒有，四個清單因此不一致：`source_columns` 把同一個欄位
    # 算了兩次、覆蓋率因此虛低，甚至可能只因為分母被灌水就觸發一個不存在的
    # E-2002；同一筆表級衝突也會被回報成兩筆，「2 個衝突」講的其實是同一件事。
    # 這裡的原則跟 `models`／`mappings` 完全一致：內容完全相同的重複收斂成
    # 一筆，因為打平的兩者本來就無法區分；只有在這一步之後，四個清單才會對
    # 同一份輸入給出彼此一致的答案。
    unmapped = _dedupe_full_equality(unmapped)
    conflicts = _dedupe_full_equality(conflicts)

    # 修正輪次 1 Important 2：先前這裡的注解宣稱「每個鍵都補到不可能打平」，
    # 但那是假的——四個鍵都只取了「一部分」欄位，掃描階段就是因為
    # stable sort + 部分鍵，靠著「輸入順序剛好穩定」這個意外性質才看起來
    # 確定，一旦輸入順序改變（例如重複表名的兩筆表換了先後），輸出就跟著變。
    # 這裡把每一個鍵都擴充到涵蓋該筆記錄的**所有**欄位，讓兩筆「內容不同」
    # 的紀錄不可能打平；兩筆內容完全相同的紀錄（例如同名重複表產生的兩個
    # 一模一樣的 ModelMapping）打平沒關係，因為打平的兩者本來就無法區分。
    #
    # 修正輪次 2 Important 1：上面那句「每個鍵都涵蓋所有欄位」在修正輪次 1
    # 對 `models` 其實不成立——`reasons` 被漏掉了。`_resolve_target_model`
    # 在表名被 `str()` 強制轉換時（`coercion_note`）會多附一行理由，兩張
    # 表名分別是 `42`（int）與 `"42"`（str）都命中同一條表規則、打平在
    # `(source_table, target_model, confidence)` 上，卻只有其中一個有
    # coercion 附註——內容不同，卻是「距離」不能區分的兩筆紀錄，輸出順序因此
    # 又變回依賴輸入順序。這裡把 `reasons` 併入鍵，才是真的涵蓋 `ModelMapping`
    # 的每一個欄位（`tier` 是 `confidence` 的純函式，不需要另外放進鍵）。
    models.sort(key=lambda m: (m.source_table, m.target_model, m.confidence, tuple(m.reasons)))
    # `FieldMapping` 還有兩個欄位這裡沒有直接列出：`tier`（`confidence` 的純
    # 函式，不可能製造新的並列）與 `transform`／`reasons`。逐一檢查過這個引擎
    # 目前所有會建構 `FieldMapping` 的路徑：只要 `source.table`／`column`
    # 與 `target.model`／`field` 都打平，`reasons`（由 `col_name`／`field_name`
    # ／`col_type`／`field_obj.type`／表的猜測 provenance 推算而得，這些全部
    # 已經在鍵裡）與 `transform`（只有 `_apply_column_rule` 會給非預設值，且
    # 是同一條 `column_rule`，由同一個 `(table, column)` 鍵查出）都會跟著
    # 打平，不會有「鍵相同、內容不同」的兩筆紀錄。即便如此，仍把兩者併入
    # 鍵中作為縱深防禦（跟 `models` 這次翻車的教訓一致：不要只靠「目前的
    # 程式碼路徑推不出反例」去確定總序，鍵本身就該字面涵蓋每個欄位）。
    # `transform` 內含 `dict | None`／`Any` 型別的欄位，不能直接排序，用
    # `repr()` 轉成可比較的字串——`Transform` 是 dataclass，預設 `repr()`
    # 會逐欄位印出內容，同樣輸入永遠得到同樣字串，不影響確定性。
    mappings.sort(
        key=lambda fm: (
            fm.source.table,
            fm.source.column,
            fm.target.model,
            fm.target.field,
            fm.source.type,
            fm.confidence,
            fm.target.type,
            tuple(fm.reasons),
            repr(fm.transform),
        )
    )
    # `UnmappedColumn` 只有 `table`／`column`／`type`／`reason` 四個欄位，
    # 上面這個鍵字面上就涵蓋了全部——不需要額外檢查或縱深防禦。
    unmapped.sort(key=lambda u: (u.table, u.column, u.type, u.reason))
    # conflicts 的 `column` 現在可能是 `None`（表級衝突，見 Important 3 的
    # `kind` 判別欄位），`None` 不能直接與 `str` 比較，這裡用 `or ""` 只在
    # 排序鍵裡把它當成空字串——這不影響輸出裡真正的 `column` 欄位（那裡仍然
    # 是 `None`）。這樣做不會與「欄位級衝突剛好欄位名稱是空字串」搞混：
    # Critical 1 修正後，空字串欄位名一律在 `_normalize_columns` 被歸類為
    # 「畸形欄位」，直接進 `unmapped`，永遠不會走到欄位級衝突這條路，所以
    # `column is None`（表級）與「排序鍵裡的 `""`」不會與任何真正欄位級
    # 衝突的欄位名稱衝突。額外把 `kind` 也放進鍵裡，讓這個不變量不成立時
    # 排序仍然是全序，而不是靜默依賴上述前提。
    # `MappingConflict` 的五個欄位（table／column／kind／candidates／reason）
    # 這裡字面上全數在鍵中（`candidates` 轉成 `tuple` 才可排序），同樣不需要
    # 額外的縱深防禦。
    conflicts.sort(
        key=lambda c: (c.table, c.kind.value, c.column or "", tuple(c.candidates), c.reason)
    )

    for conflict in conflicts:
        event_log.emit(
            "map.conflict",
            code=E_2001,
            message=f"{conflict.table}.{conflict.column or '(整張表)'}：{conflict.reason}",
            details={
                "table": conflict.table,
                "column": conflict.column,
                "candidates": conflict.candidates,
            },
        )

    stats = _build_stats(mappings, unmapped, conflicts)
    if stats.coverage_percent < _COVERAGE_WARN_THRESHOLD:
        event_log.emit(
            "map.coverage_low",
            code=E_2002,
            message=(
                f"欄位覆蓋率為 {stats.coverage_percent}%，低於 {_COVERAGE_WARN_THRESHOLD}% 門檻"
            ),
            details={"coverage_percent": stats.coverage_percent},
        )

    return Mapping(
        schema_version=SCHEMA_VERSION,
        inventory_synthetic=inventory_synthetic,
        stats=stats,
        models=models,
        mappings=mappings,
        unmapped=unmapped,
        conflicts=conflicts,
    )


def _resolve_duplicate_model_mappings(
    models_in: list[ModelMapping], conflicts_out: list[MappingConflict]
) -> list[ModelMapping]:
    """修正輪次 3 Critical 1：把 `models_in` 按 `source_table` 分組，處理重複
    表名帶來的重複 `ModelMapping`。

    每組只有一筆 → 原樣保留。超過一筆時，比對「核心事實」
    （`target_model`、`confidence`）：

    - 核心事實一致（只有 `reasons` 不同，例如其中一次表名被強制轉換而多了
      一行附註）→ 這不是真的矛盾，兩者對「這張表對到哪個模型、有多確定」
      講的是同一件事。把所有 `reasons` 聯集、排序後合併成一筆——不得意
      挑一份、丟掉另一份携帶的資訊，也不該把「附註多寡不同」誤報成衝突
      （本專案的原則是「不得回報不存在的問題」）。排序而非保留原始順序，
      是為了讓合併結果不依賴哪一張重複表先被處理到。
    - 核心事實不一致（不同的 `target_model` 或 `confidence`）→ 這才是真正
      的矛盾：同一個表名被判定出兩種不同的事實，不能任選一邊，也不能兩者
      都當作可信的 `ModelMapping` 同時輸出。改記一筆表級 `MappingConflict`，
      這個 `source_table` 不產生任何 `ModelMapping`。

    最終整分支審查修正 Minor 7：下面「核心事實不一致」那個分支，經由
    `build_mapping` 這個公開路徑實際上**不可能被觸發**——`_resolve_target_
    model` 是 `table_name`／`catalog`／`rules` 的純函式，同一個
    `table_name` 不管在 `models_in` 裡出現幾次，每次呼叫的輸入完全相同，
    輸出（`target_model`／`confidence`）必然相同，兩筆同名表的
    `ModelMapping` 因此永遠落在「核心事實一致」那一支，不會走到這裡。這個
    分支是縱深防禦（防止未來若有人改動呼叫順序、讓 `_resolve_target_model`
    對同一張表被非純粹地呼叫兩次，仍然有安全網接住），刻意保留、不刪除，
    但誠實標記為「不可由 `build_mapping` 觸達」——涵蓋它的是
    `tests/mapping/test_engine.py` 直接呼叫這個私有函式的單元測試，而不是
    透過 `build_mapping` 的端到端案例。
    """
    grouped: dict[str, list[ModelMapping]] = {}
    order: list[str] = []
    for model_mapping in models_in:
        if model_mapping.source_table not in grouped:
            grouped[model_mapping.source_table] = []
            order.append(model_mapping.source_table)
        grouped[model_mapping.source_table].append(model_mapping)

    resolved: list[ModelMapping] = []
    for source_table in order:
        group = grouped[source_table]
        if len(group) == 1:
            resolved.append(group[0])
            continue

        cores = {(m.target_model, m.confidence) for m in group}
        if len(cores) == 1:
            merged_reasons = sorted({reason for m in group for reason in m.reasons})
            resolved.append(replace(group[0], reasons=merged_reasons))
            continue

        candidates = sorted({m.target_model for m in group})
        conflicts_out.append(
            MappingConflict(
                table=source_table,
                column=None,
                kind=ConflictKind.TABLE,
                candidates=candidates,
                reason=(
                    f"資料表 `{source_table}` 在來源 inventory 中重複出現超過一次"
                    "（`inventory.schema.json` 的 `tables` 陣列沒有 `uniqueItems` 限制，"
                    "`scan` 也不去重），且各次出現判定出的目標模型或信心不一致"
                    f"（候選：{candidates}）——不能同時把內容矛盾、鍵卻相同的兩筆 "
                    "ModelMapping 都當作可信的事實回報，改記為表級衝突，不產生 ModelMapping"
                ),
            )
        )
    return resolved


def _resolve_duplicate_field_mappings(
    mappings_in: list[FieldMapping], conflicts_out: list[MappingConflict]
) -> list[FieldMapping]:
    """修正輪次 3 Critical 1：把 `mappings_in` 按 `(source.table, source.column)`
    分組，處理重複表名帶來的重複 `FieldMapping`（controller 重現的核心案例：
    同一個表名被定義兩次、同名欄位型別互相矛盾，例如 `cust_no` 一次是
    `CHAR(10)`、一次是 `INTEGER`，兩次卻都以 confidence 100 套用同一條
    欄位規則）。

    每組只有一筆 → 原樣保留。超過一筆時，逐筆比對完整內容（`FieldMapping`
    是 dataclass，`==` 會逐欄位比較，包含巢狀的 `source`／`target`／
    `transform`）：

    - 完全相同 → 去重成一筆，而不是把同一個事實輸出兩次。
    - 內容不同（型別、目標欄位或信心至少有一項不一致）→ 這是真正的矛盾：
      同一個來源欄位鍵被賦予兩種不同的事實，不能兩者都當成信心 100 的
      `FieldMapping` 靜靜輸出。改記一筆欄位級 `MappingConflict`，這個
      `(table, column)` 鍵不產生任何 `FieldMapping`。
    """
    grouped: dict[tuple[str, str], list[FieldMapping]] = {}
    order: list[tuple[str, str]] = []
    for field_mapping in mappings_in:
        key = (field_mapping.source.table, field_mapping.source.column)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(field_mapping)

    resolved: list[FieldMapping] = []
    for key in order:
        group = grouped[key]
        if len(group) == 1:
            resolved.append(group[0])
            continue

        distinct: list[FieldMapping] = []
        for field_mapping in group:
            if field_mapping not in distinct:
                distinct.append(field_mapping)
        if len(distinct) == 1:
            resolved.append(distinct[0])
            continue

        table, column = key
        candidates = sorted({f"{fm.target.model}.{fm.target.field}" for fm in group})
        source_types = sorted({fm.source.type for fm in group})
        conflicts_out.append(
            MappingConflict(
                table=table,
                column=column,
                kind=ConflictKind.COLUMN,
                candidates=candidates,
                reason=(
                    f"資料表 `{table}` 在來源 inventory 中重複出現，欄位 `{column}` 在"
                    f"不同次出現中被賦予互相矛盾的結果（來源型別：{source_types}；候選："
                    f"{candidates}）——不能同時把內容矛盾、鍵卻相同的 FieldMapping 都當作"
                    "可信的事實回報，改記為欄位級衝突，不產生 FieldMapping"
                ),
            )
        )
    return resolved


def _detect_target_field_conflicts(
    mappings: list[FieldMapping], conflicts_out: list[MappingConflict]
) -> None:
    """Task 4.5（昴君裁決）：同一張來源表裡，若有一個以上的來源欄位都對應到
    同一個目標欄位（`模型.欄位`），記一筆 `ConflictKind.TARGET` 衝突。

    只在**同一張來源表內**比對——不同來源表對到同一個 Odoo 欄位是正常的，
    它們會變成不同的記錄，不該被誤報。跨 tier 都要抓：一筆 `auto` 加一筆
    `needs_human` 也是衝突（corpus case03 的 `ord_mast.ord_no`／
    `ord_mast.ord_cust_ref` 都對到 `sale.order.client_order_ref` 正是這個
    形狀）。`unmapped` 的欄位本來就不在 `mappings` 清單裡，天然被排除。

    這裡不會把命中的 `FieldMapping` 從 `mappings` 移除——衝突是「該用哪一個」
    的裁決問題，不是任一筆本身算錯了，兩筆都要保留讓人類看到完整事實，只是
    多記一筆衝突提醒「這裡需要人工裁定」。`_build_stats` 因此不需要調整：
    這些來源欄位仍然透過 `mappings` 被計入 `mapped_columns`，不能再算進
    `source_columns` 的分母一次，否則會重複計算。
    """
    grouped: dict[tuple[str, str, str], list[str]] = {}
    order: list[tuple[str, str, str]] = []
    for fm in mappings:
        key = (fm.source.table, fm.target.model, fm.target.field)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(fm.source.column)

    for key in order:
        table, target_model, target_field = key
        columns = sorted(set(grouped[key]))
        if len(columns) <= 1:
            continue
        conflicts_out.append(
            MappingConflict(
                table=table,
                column=None,
                kind=ConflictKind.TARGET,
                candidates=columns,
                reason=(
                    f"資料表 `{table}` 有 {len(columns)} 個來源欄位"
                    f"（{'、'.join(columns)}）同時對應到目標欄位 "
                    f"`{target_model}.{target_field}`——載入階段寫入同一筆記錄時，"
                    "後寫入的值會覆蓋先寫入的，且沒有機制決定哪一個是正確答案，"
                    "需要人工裁定保留哪一個來源欄位"
                ),
            )
        )


def _dedupe_full_equality(items: list[Any]) -> list[Any]:
    """把 `items` 依照完整內容（dataclass 預設 `==` 逐欄位比較）去重，保留
    第一次出現的順序（最終整分支審查修正 Important 1）。

    只做「完全相同才收斂成一筆」這一件事——不像 `_resolve_duplicate_model_
    mappings`／`_resolve_duplicate_field_mappings` 還要處理「核心事實一致但
    附註不同該合併」「核心事實矛盾該改記衝突」這兩種更複雜的情形。`unmapped`／
    `conflicts` 不需要那兩種情形：`UnmappedColumn`／`MappingConflict` 本身
    已經是「這裡有問題」的最終陳述，不會有「同一個鍵、事實不同」需要另外
    仲裁的空間——真正矛盾的情形（例如重複表名判定出不同模型）早在
    `_resolve_duplicate_model_mappings`／`_resolve_duplicate_field_mappings`
    產生新的衝突時就已經處理過；這裡只需要把「同一件事被記錄了兩次」的
    純重複收斂掉。`O(n^2)` 的 `in` 檢查在此可接受：一份 inventory 裡欄位級
    問題的筆數是人工審核可負擔的規模，不是效能敏感路徑。
    """
    deduped: list[Any] = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _validate_inventory(inventory: Any, event_log: EventLog) -> None:
    if not isinstance(inventory, dict):
        missing = list(_REQUIRED_INVENTORY_KEYS)
    else:
        missing = [key for key in _REQUIRED_INVENTORY_KEYS if key not in inventory]

    if missing:
        message = f"inventory 缺少必要鍵：{', '.join(missing)}"
        event_log.emit(
            "map.inventory_invalid",
            code=E_2001,
            message=message,
            details={"missing_keys": missing},
        )
        raise MapFailed(message)


def _normalize_columns(columns_raw: Any) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """把一張表的 `columns` 原始值整理成 `(合法欄位清單, 畸形欄位清單)`。

    回傳 `(valid, malformed)`，兩者都是 `(顯示用名稱, 型別字串)` 的清單：

    - `valid`：`name` 是非空字串的欄位，繼續走 rules／catalog 比對的正常路徑。
    - `malformed`：欄位本身是 dict，但 `name` 缺失或不是非空字串
      （修正輪次 1 Critical 1）。`inventory.schema.json` 的 `$defs/column.name`
      只要求 `type: string`，沒有 `minLength`，所以 `{"name": ""}` 是
      schema-valid 輸入——這種欄位**確實存在**，不能悄悄消失，只是沒有
      任何合法的字串可以拿去跟規則或 catalog 比對。呼叫端要把這裡的每一筆
      直接送進 `unmapped`：`""` 用字面值本身表示，非字串（例如 `None`、
      `123`）用 `repr()`，讓輸出忠實呈現原始資料實際長什麼樣。

    只有「連 dict 都不是」的欄位項目才真正被丟棄——那種輸入連一個可以代表
    它的名稱字串都沒有（`inventory.schema.json` 的 `$defs/column` 本身要求
    是 object，這是唯一防禦畸形輸入、非 schema-valid 路徑）。`type` 缺失或
    為 `None` 一律當成空字串，而不是跳過——欄位本身的存在是確定的，只是
    型別未知，仍要讓它走完整條 unmapped／conflict／mapping 的路徑。
    """
    if not isinstance(columns_raw, list):
        return [], []

    valid: list[tuple[str, str]] = []
    malformed: list[tuple[str, str]] = []
    for column in columns_raw:
        if not isinstance(column, dict):
            continue
        raw_type = column.get("type")
        col_type = raw_type if isinstance(raw_type, str) else ""
        name = column.get("name")
        if isinstance(name, str) and name:
            valid.append((name, col_type))
            continue
        display_name = name if isinstance(name, str) else repr(name)
        malformed.append((display_name, col_type))
    return valid, malformed


def _normalize_foreign_keys(
    foreign_keys_raw: Any,
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """把一張表的 `foreign_keys` 原始值整理成
    `(單欄位 → (ref_table, ref_column) 對照, 多欄外鍵涉及的欄位集合)`。

    第一個回傳值涵蓋**所有**外鍵（不論欄位數），依欄位在 `columns` 陣列中的
    位置對到 `ref_columns` 同一位置——這是給規則衝突檢查用的：即使某個欄位
    屬於多欄外鍵，只要規則宣告了它的 `reference`，仍然要能比對規則與 DDL
    是否一致（行為契約的衝突條件沒有限定只適用單欄外鍵）。

    第二個回傳值只收「屬於某條 `columns` 長度 > 1 的外鍵」的欄位名稱——呼叫
    端用它決定：沒有規則時，這個欄位要被強制送進 `unmapped`（MVP 不自動對
    多欄外鍵產生 reference），而不是悄悄落回 identity 把外鍵原始碼寫進整數
    關聯欄位。

    對任何畸形輸入（不是 list、外鍵項目不是 dict、`columns`／`ref_columns`
    長度不一致或缺失、內含非字串元素）一律安全跳過該筆外鍵，不拋出——與
    `_normalize_columns` 的防禦原則一致：inventory 已經過 schema 驗證，這裡
    只是再做一次最小限度的防禦。同一個欄位若出現在一條以上的外鍵裡（schema
    沒有明文禁止），保留第一次遇到的對照，讓行為對輸入順序保持決定性。
    """
    ref_by_column: dict[str, tuple[str, str]] = {}
    multi_column_columns: set[str] = set()

    if not isinstance(foreign_keys_raw, list):
        return ref_by_column, multi_column_columns

    for fk in foreign_keys_raw:
        if not isinstance(fk, dict):
            continue
        columns = fk.get("columns")
        ref_table = fk.get("ref_table")
        ref_columns = fk.get("ref_columns")
        if not isinstance(columns, list) or not isinstance(ref_columns, list):
            continue
        if not isinstance(ref_table, str) or len(columns) != len(ref_columns):
            continue

        is_multi_column = len(columns) > 1
        for col, ref_col in zip(columns, ref_columns, strict=True):
            if not isinstance(col, str) or not isinstance(ref_col, str):
                continue
            if is_multi_column:
                multi_column_columns.add(col)
            ref_by_column.setdefault(col, (ref_table, ref_col))

    return ref_by_column, multi_column_columns


def _process_table(
    table_entry: Any,
    catalog: dict[str, OdooModel],
    rules: RuleSet,
    models_out: list[ModelMapping],
    mappings_out: list[FieldMapping],
    unmapped_out: list[UnmappedColumn],
    conflicts_out: list[MappingConflict],
) -> None:
    if not isinstance(table_entry, dict):
        # 連 dict 都不是，沒有任何可以辨識的欄位結構，沒有東西可以歸屬、
        # 也沒有東西會「消失」——這是唯一一種真的無法產生任何輸出的輸入。
        return

    raw_name = table_entry.get("name")
    # 修正輪次 1 Minor 7：非字串的表名以前被硬轉成 `""`，原始值從此在輸出與
    # 事件裡都看不到。這裡改用 `str(raw_name)` 保留可辨識的值，並記一筆
    # 「已強制轉換」的附註，之後會附進這張表的模型判斷理由裡（見下）。
    if isinstance(raw_name, str):
        table_name = raw_name
        coercion_note: str | None = None
    else:
        table_name = str(raw_name)
        coercion_note = (
            f"資料表名稱不是字串（原始值 {raw_name!r}），已強制轉換為字串 `{table_name}` "
            "以避免這張表的資訊整個消失"
        )

    columns, malformed_columns = _normalize_columns(table_entry.get("columns"))
    fk_ref_by_column, multi_column_fk_columns = _normalize_foreign_keys(
        table_entry.get("foreign_keys")
    )

    for display_name, col_type in malformed_columns:
        unmapped_out.append(
            UnmappedColumn(
                table=table_name,
                column=display_name,
                type=col_type,
                reason=_MALFORMED_COLUMN_NAME_REASON,
            )
        )

    table_rule = rules.table_rules.get(table_name)
    target_model, no_model_reason, heuristic_confidence = _resolve_target_model(
        table_name, table_rule, catalog, rules, models_out, conflicts_out, coercion_note
    )

    if target_model is None:
        for col_name, col_type in columns:
            unmapped_out.append(
                UnmappedColumn(
                    table=table_name, column=col_name, type=col_type, reason=no_model_reason
                )
            )
        return

    # 修正輪次 1 Minor 9：這張表如果是靠表名啟發式猜出來的（不是規則、也不是
    # 從欄位規則推論），底下每一筆 FieldMapping 都建立在這個猜測之上——
    # 即使欄位本身對得很準（confidence 100），表的選擇仍然是猜的。這裡只加
    # 一行 provenance 附註，不改變任何 confidence／tier。
    heuristic_note: str | None = None
    if heuristic_confidence is not None:
        heuristic_note = (
            f"此欄位所屬資料表 `{table_name}` 對應到模型 `{target_model}` 是依表名啟發式猜測而來"
            f"（confidence={heuristic_confidence}），不是規則指定、也不是從欄位規則推論所得"
        )

    for col_name, col_type in columns:
        _process_column(
            table_name,
            col_name,
            col_type,
            target_model,
            catalog,
            rules,
            mappings_out,
            unmapped_out,
            conflicts_out,
            heuristic_note,
            heuristic_confidence,
            fk_ref_by_column.get(col_name),
            col_name in multi_column_fk_columns,
        )


def _append_model_mapping(
    models_out: list[ModelMapping],
    *,
    source_table: str,
    target_model: str,
    confidence: int,
    reason: str,
    coercion_note: str | None,
) -> None:
    """組出一筆 `ModelMapping` 並附加到 `models_out`（Minor 2，行為不變的小重
    構）。`_resolve_target_model` 與 `_infer_model_from_column_rules` 有三處
    「組 reasons（若表名被強制轉換，多附一行附註）→ 建構 ModelMapping →
    append」的重複，抽成這個小函式；不改變任何一個呼叫點的 confidence／
    tier／reasons 內容。
    """
    reasons = [reason]
    if coercion_note:
        reasons.append(coercion_note)
    models_out.append(
        ModelMapping(
            source_table=source_table,
            target_model=target_model,
            confidence=confidence,
            tier=tier_for(confidence),
            reasons=reasons,
        )
    )


def _resolve_target_model(
    table_name: str,
    table_rule: TableRule | None,
    catalog: dict[str, OdooModel],
    rules: RuleSet,
    models_out: list[ModelMapping],
    conflicts_out: list[MappingConflict],
    coercion_note: str | None,
) -> tuple[str | None, str, int | None]:
    """決定這張表對到哪個 Odoo 模型。

    回傳 `(target_model, reason, heuristic_confidence)`：

    - `target_model` 為 `None` 時，`reason` 是這張表的欄位要放進 `unmapped`
      時共用的理由字串（呼叫端不需要再判斷是哪一種對不上）。
    - `heuristic_confidence` 只有在 `target_model` 是靠表名啟發式（`name_score`
      對 catalog 唯一勝出）判出來時才非 `None`；命中規則或從欄位規則推論
      出來的模型都不算「猜」，回傳 `None`（Minor 9 的判別依據）。
    """
    if table_rule is not None:
        _append_model_mapping(
            models_out,
            source_table=table_name,
            target_model=table_rule.target_model,
            confidence=100,
            reason=f"命中資料表規則：{table_rule.origin}",
            coercion_note=coercion_note,
        )
        return table_rule.target_model, "", None

    scored: list[tuple[str, int, str]] = []
    for model_name in catalog:
        last_segment = model_name.rsplit(".", 1)[-1]
        score, reason = name_score(table_name, last_segment)
        scored.append((model_name, round(100 * score), reason))

    best_confidence = max((confidence for _, confidence, _ in scored), default=0)

    if best_confidence > 0:
        best_models = sorted(
            name for name, confidence, _ in scored if confidence == best_confidence
        )
        if len(best_models) > 1:
            reason = (
                f"表名與 {len(best_models)} 個 Odoo 模型的最後一段同分並列"
                f"（confidence={best_confidence}），無法唯一決定 target_model"
            )
            if coercion_note:
                reason = f"{reason}；{coercion_note}"
            conflicts_out.append(
                MappingConflict(
                    table=table_name,
                    column=None,
                    kind=ConflictKind.TABLE,
                    candidates=best_models,
                    reason=reason,
                )
            )
            return None, _MODEL_TIE_UNMAPPED_REASON, None

        target_model = best_models[0]
        reason = next(reason for name, _, reason in scored if name == target_model)
        _append_model_mapping(
            models_out,
            source_table=table_name,
            target_model=target_model,
            confidence=best_confidence,
            reason=reason,
            coercion_note=coercion_note,
        )
        return target_model, "", best_confidence

    # best_confidence == 0（含 catalog 為空的情況）：表名啟發式完全判不出
    # 模型。修正輪次 1 Important 4（人類裁定）：在放棄之前，先看這張表是否
    # 有欄位規則——那是人類明確教過的知識，不該因為表本身沒有規則就被略過
    # （`rules/README.md` 明講 `tables` 是可選的）。
    inferred = _infer_model_from_column_rules(
        table_name, rules, coercion_note, models_out, conflicts_out
    )
    if inferred is not None:
        target_model, reason = inferred
        return target_model, reason, None
    return None, _NO_MODEL_MATCH_REASON, None


def _infer_model_from_column_rules(
    table_name: str,
    rules: RuleSet,
    coercion_note: str | None,
    models_out: list[ModelMapping],
    conflicts_out: list[MappingConflict],
) -> tuple[str | None, str] | None:
    """當一張表沒有 table rule、表名啟發式也完全判不出模型時，看這張表的
    欄位規則能不能把模型推論出來（修正輪次 1 Important 4，人類裁定）。

    回傳 `None` 代表這張表根本沒有任何欄位規則——呼叫端應該落回「完全對不上」
    的舊行為，不改變既有的兩個 controller-verified baseline。

    回傳 `(target_model, reason)`：
    - 該表所有欄位規則的 `target_model` 一致 → 推論出這個模型，
      `confidence = 100`、`reason = ""`（呼叫端不需要這個分支的 unmapped
      理由，因為根本不會產生 unmapped——表已經對到模型了）。理由記錄在
      `ModelMapping.reasons` 裡，且明講這是「推論」而非名稱猜測
      （猜測要標明是猜測；這裡反過來也要標明「不是」猜測，而是人類知識）。
    - 欄位規則彼此指向不同模型 → 這是真正的矛盾：發一筆表級 `MappingConflict`
      （`kind = ConflictKind.TABLE`，`candidates` 是排序過的競爭模型清單），
      回傳 `(None, _COLUMN_RULES_CONTRADICT_REASON)`。
    """
    matching_rules = sorted(
        (
            rule
            for (source_table, _), rule in rules.column_rules.items()
            if source_table == table_name
        ),
        key=lambda rule: rule.origin,
    )
    if not matching_rules:
        return None

    distinct_models = sorted({rule.target_model for rule in matching_rules})
    origins = [rule.origin for rule in matching_rules]

    if len(distinct_models) == 1:
        target_model = distinct_models[0]
        reason = (
            f"資料表 `{table_name}` 本身沒有對到任何模型（沒有資料表規則，表名與所有 catalog "
            f"模型的名稱啟發式評分也都是 0），但其欄位規則（{', '.join(origins)}）一致指向 "
            f"`{target_model}`，因此從欄位規則推論這張表對應到該模型——這是推論，不是名稱猜測"
        )
        _append_model_mapping(
            models_out,
            source_table=table_name,
            target_model=target_model,
            confidence=100,
            reason=reason,
            coercion_note=coercion_note,
        )
        return target_model, ""

    conflict_reason = (
        f"資料表 `{table_name}` 本身沒有對到任何模型，其欄位規則又指向不同模型 "
        f"{distinct_models}（{', '.join(origins)}），無法唯一決定 target_model"
    )
    if coercion_note:
        conflict_reason = f"{conflict_reason}；{coercion_note}"
    conflicts_out.append(
        MappingConflict(
            table=table_name,
            column=None,
            kind=ConflictKind.TABLE,
            candidates=distinct_models,
            reason=conflict_reason,
        )
    )
    return None, _COLUMN_RULES_CONTRADICT_REASON


def _propagate_table_uncertainty(
    field_confidence: int, heuristic_confidence: int | None, reasons: list[str]
) -> tuple[int, Tier]:
    """Important 3（人類裁定，修正輪次 3）：confidence 是乘積訊號——表→模型
    這一層如果也是猜的，欄位這一層就不能假裝那件事沒發生，原樣輸出 100。

    `heuristic_confidence` 為 `None`（表的模型是命中規則、或從欄位規則推論
    而來，兩者都不是「猜」）時原樣回傳，不動 `reasons`。否則把這個欄位的
    confidence 乘上表的猜測信心（`round(field_confidence * heuristic_confidence
    / 100)`），並把算式本身寫進 `reasons`，讓人類看得懂數字是怎麼來的——
    `reasons` 是人工審核的唯一依據，光說「打折了」不夠，要能重算一次核對。
    `tier` 一律用打折後的 confidence 重新算，不能停留在打折前的分層。
    """
    if heuristic_confidence is None:
        return field_confidence, tier_for(field_confidence)

    propagated = round(field_confidence * heuristic_confidence / 100)
    reasons.append(
        f"此欄位所屬資料表的模型判定信心只有 {heuristic_confidence}"
        "（依表名啟發式猜測而來，見上一則理由），此欄位信心依此打折："
        f"round({field_confidence} × {heuristic_confidence} / 100) = {propagated}"
    )
    return propagated, tier_for(propagated)


def _process_column(
    table_name: str,
    col_name: str,
    col_type: str,
    target_model: str,
    catalog: dict[str, OdooModel],
    rules: RuleSet,
    mappings_out: list[FieldMapping],
    unmapped_out: list[UnmappedColumn],
    conflicts_out: list[MappingConflict],
    heuristic_note: str | None,
    heuristic_confidence: int | None,
    fk_ref: tuple[str, str] | None,
    is_multi_column_fk: bool,
) -> None:
    column_rule = rules.column_rules.get((table_name, col_name))
    if column_rule is not None:
        _apply_column_rule(
            table_name,
            col_name,
            col_type,
            target_model,
            column_rule,
            catalog,
            mappings_out,
            conflicts_out,
            heuristic_note,
            heuristic_confidence,
            fk_ref,
        )
        return

    # Task 3 行為契約：規則沒宣告 transform、但這個欄位屬於一條多欄（複合）
    # 外鍵——MVP 不自動產生 reference（無法只靠單一欄位的值組出被參照記錄
    # 完整的 external_id）。直接進 unmapped，不能讓它繼續往下走到底下的
    # 名稱／型別啟發式，那會產生一筆看起來合法、實則把外鍵原始碼當成一般值
    # 寫進 Odoo 整數關聯欄位的 identity FieldMapping。
    if is_multi_column_fk:
        unmapped_out.append(
            UnmappedColumn(
                table=table_name,
                column=col_name,
                type=col_type,
                reason=_MULTI_COLUMN_FK_UNSUPPORTED_REASON,
            )
        )
        return

    # 理由具體化（審查與控制者複驗定案）：這個欄位如果有 DDL 外鍵宣告，
    # 「找不到相容欄位」的通用理由會浪費 DDL 已知的資訊——改用
    # `_fk_declared_no_match_reason` 點名被參照的表與欄位，並指向 rules/。
    no_match_reason = (
        _fk_declared_no_match_reason(*fk_ref) if fk_ref is not None else _NO_FIELD_MATCH_REASON
    )

    model = catalog.get(target_model)
    if model is None or not model.fields:
        unmapped_out.append(
            UnmappedColumn(table=table_name, column=col_name, type=col_type, reason=no_match_reason)
        )
        return

    scored: list[tuple[str, int, str, str]] = []
    for field_name, field_obj in model.fields.items():
        n_score, n_reason = name_score(col_name, field_name)
        t_score, t_reason = type_score(col_type, field_obj.type)
        confidence = round(100 * n_score * t_score)
        scored.append((field_name, confidence, n_reason, t_reason))

    best_confidence = max(confidence for _, confidence, _, _ in scored)
    if best_confidence == 0:
        unmapped_out.append(
            UnmappedColumn(table=table_name, column=col_name, type=col_type, reason=no_match_reason)
        )
        return

    best_fields = sorted(name for name, confidence, _, _ in scored if confidence == best_confidence)
    if len(best_fields) > 1:
        conflicts_out.append(
            MappingConflict(
                table=table_name,
                column=col_name,
                kind=ConflictKind.COLUMN,
                candidates=[f"{target_model}.{field_name}" for field_name in best_fields],
                reason=(
                    f"{len(best_fields)} 個 `{target_model}` 欄位並列最高分"
                    f"（confidence={best_confidence}），無法唯一決定對應目標"
                ),
            )
        )
        return

    field_name = best_fields[0]
    _, _, n_reason, t_reason = next(entry for entry in scored if entry[0] == field_name)
    field_obj = model.fields[field_name]

    reasons = [n_reason, t_reason]
    if heuristic_note:
        reasons.append(heuristic_note)
    # 昴君裁決：這欄若有 DDL 外鍵宣告，即使已經成功對應（不是進 unmapped），
    # 也要在 reasons 點名——名稱重疊率的計算不會告訴人這其實是外鍵。
    if fk_ref is not None:
        reasons.append(_fk_declared_reason_for_mapped_field(*fk_ref))

    # 不再從 `fk_ref` 自動產生 `reference`（見模組層級「修正」註解說明原因）
    # ——沒有規則時一律用 `identity`，跟 Task 3 之前的行為一致。
    transform = Transform(kind=TransformKind.IDENTITY)

    confidence, tier = _propagate_table_uncertainty(best_confidence, heuristic_confidence, reasons)
    mappings_out.append(
        FieldMapping(
            source=SourceRef(table=table_name, column=col_name, type=col_type),
            target=TargetRef(model=target_model, field=field_name, type=field_obj.type),
            transform=transform,
            confidence=confidence,
            tier=tier,
            reasons=reasons,
        )
    )


def _apply_column_rule(
    table_name: str,
    col_name: str,
    col_type: str,
    target_model: str,
    column_rule: ColumnRule,
    catalog: dict[str, OdooModel],
    mappings_out: list[FieldMapping],
    conflicts_out: list[MappingConflict],
    heuristic_note: str | None,
    heuristic_confidence: int | None,
    fk_ref: tuple[str, str] | None,
) -> None:
    if column_rule.target_model != target_model:
        conflicts_out.append(
            MappingConflict(
                table=table_name,
                column=col_name,
                kind=ConflictKind.COLUMN,
                candidates=[f"{column_rule.target_model}.{column_rule.target_field}"],
                reason=(
                    f"規則 {column_rule.origin} 將此欄位指到模型 `{column_rule.target_model}`，"
                    f"但資料表 `{table_name}` 已對應到模型 `{target_model}`，兩者不一致"
                ),
            )
        )
        return

    # Task 3 衝突條件：規則明確宣告了 transform（優先順序第 1 條），但如果
    # 規則說的是 `reference`、DDL 也宣告了同一欄位的外鍵，且兩邊的
    # `ref_table`／`ref_column` 不一致——這是規則與 DDL 兩個事實來源互相
    # 打架，不能挑一邊猜，要發 E-2001 讓人類裁定。兩邊一致（保留，不重複
    # 記）或只有一邊宣告（用那一邊）都不算衝突，所以只在兩者都存在且不一致
    # 時才進這個分支。
    if (
        column_rule.transform.kind == TransformKind.REFERENCE
        and fk_ref is not None
        and (column_rule.transform.ref_table, column_rule.transform.ref_column) != fk_ref
    ):
        ddl_ref_table, ddl_ref_column = fk_ref
        conflicts_out.append(
            MappingConflict(
                table=table_name,
                column=col_name,
                kind=ConflictKind.COLUMN,
                candidates=[
                    f"規則（{column_rule.origin}）：ref_table=`{column_rule.transform.ref_table}`, "
                    f"ref_column=`{column_rule.transform.ref_column}`",
                    f"DDL 的 foreign_keys：ref_table=`{ddl_ref_table}`, "
                    f"ref_column=`{ddl_ref_column}`",
                ],
                reason=(
                    f"規則 {column_rule.origin} 宣告此欄位 transform 為 reference，參照 "
                    f"`{column_rule.transform.ref_table}.{column_rule.transform.ref_column}`；"
                    f"但來源 DDL 的 foreign_keys 宣告此欄位參照 "
                    f"`{ddl_ref_table}.{ddl_ref_column}`，兩者不一致"
                ),
            )
        )
        return

    model = catalog.get(target_model)
    field_obj = model.fields.get(column_rule.target_field) if model is not None else None
    target_type = field_obj.type if field_obj is not None else ""

    reasons = [f"命中欄位規則：{column_rule.origin}"]
    if field_obj is None:
        # Minor 8：規則指到的欄位（或整個模型）不存在於「本次呼叫端傳入的」
        # catalog 裡。`load_rules` 會在載入規則檔當下就攔截這種情況，所以
        # 這條路徑只有呼叫端傳入與規則檔驗證時不同、較小的 catalog 時才會
        # 走到（例如測試或工具直接組出精簡 catalog）。confidence／tier 仍然
        # 是 100／auto——規則本身依然是人類明確的事實，只是這裡要讓退化
        # 狀態被看見，而不是靜默留一個空的 target.type。
        reasons.append(
            f"規則指定的目標欄位 `{column_rule.target_model}.{column_rule.target_field}` "
            "不存在於本次提供的 catalog 中，target.type 因此留空——這是非成對輸入（規則檔與 "
            "catalog 分開提供）時才會出現的退化路徑"
        )
    if heuristic_note:
        reasons.append(heuristic_note)

    confidence, tier = _propagate_table_uncertainty(100, heuristic_confidence, reasons)
    mappings_out.append(
        FieldMapping(
            source=SourceRef(table=table_name, column=col_name, type=col_type),
            target=TargetRef(
                model=column_rule.target_model, field=column_rule.target_field, type=target_type
            ),
            transform=column_rule.transform,
            confidence=confidence,
            tier=tier,
            reasons=reasons,
        )
    )


def _build_stats(
    mappings: list[FieldMapping],
    unmapped: list[UnmappedColumn],
    conflicts: list[MappingConflict],
) -> MappingStats:
    """從實際產出的清單算出統計，不維護平行計數器——避免計數與清單脫鉤漂移。

    `conflicts` 裡 `kind == ConflictKind.TABLE` 的項目是表級衝突（表對不上
    模型，或欄位規則彼此矛盾），不對應任何一個真實的來源欄位（該表的欄位
    另外各自進了 `unmapped`），所以不計入 `source_columns`。
    """
    mapped_columns = len(mappings)
    column_level_conflicts = sum(
        1 for conflict in conflicts if conflict.kind == ConflictKind.COLUMN
    )
    source_columns = mapped_columns + len(unmapped) + column_level_conflicts

    coverage_percent = 100 if source_columns == 0 else round(100 * mapped_columns / source_columns)

    auto = sum(1 for fm in mappings if fm.tier == Tier.AUTO)
    suggested = sum(1 for fm in mappings if fm.tier == Tier.SUGGESTED)
    needs_human = sum(1 for fm in mappings if fm.tier == Tier.NEEDS_HUMAN)

    return MappingStats(
        source_columns=source_columns,
        mapped_columns=mapped_columns,
        coverage_percent=coverage_percent,
        auto=auto,
        suggested=suggested,
        needs_human=needs_human,
    )
