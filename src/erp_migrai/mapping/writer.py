"""把 `Mapping` 寫成 `mapping.json`（機器契約）與 `mapping.csv`（人工審核用）。

`mapping.json` 的寫法與 `inventory.json`（`erp_migrai.inventory.extractor.
write_inventory`）刻意保持一致：`ensure_ascii=False`、`indent=2`、結尾換行、
確定性——同樣輸入必須產生逐位元組相同的輸出，這兩份檔案對一個讀者（或
`git diff`）而言要長得一樣。

**`write_mapping` 在寫出前一定會呼叫 `validate_mapping`**：掃描階段的教訓是
`inventory.py` 曾經宣告會驗證卻沒有任何呼叫者真的驗證過。這裡驗證失敗時
直接讓 `jsonschema.ValidationError` 原樣往外傳，且在驗證通過之前完全不碰
檔案系統（不建立目錄、不寫檔）——一份違反自己宣告的 schema 的
`mapping.json` 半成品，比完全沒有這份檔案更危險。

最終整分支審查修正：`json.dumps` 本身也必須排在 `out_dir.mkdir` 之前。
先前 `validate_mapping` 通過之後就立刻建立了 `out_dir`，`json.dumps` 才
接著做——`mapping.schema.json` 的 `transform.value` 刻意允許任意型別（見
schema 本身的 description），若一條規則的 `value` 是 YAML 解析出來、
`json.dumps` 不支援的型別（`rules_loader._ensure_json_serializable` 是
第一道防線，這裡是第二道，避免任何未來繞過第一道防線的路徑仍然默默產出
半成品），使用者會拿到一個已經建立好、但只有 `events.jsonl` 沒有
`mapping.json` 的 `out/`，違反本模組自己的「驗證先於任何檔案系統操作」
承諾。現在 `json.dumps` 失敗會拋 `MappingSerializationError`，且完全發生
在 `mkdir` 之前。

`mapping.csv` 是給人在試算表裡審核與編輯用的：欄位順序固定（見
`CSV_HEADER`），`unmapped`／`conflicts` 也各自進一列——只列出對上的欄位會
讓人誤以為覆蓋率是 100%，那正是本專案要杜絕的「悄悄產生不完整的結果」。

CSV 公式注入：`source_table`／`source_column` 等欄位的內容來自客戶自己的
DDL，風險本來就低，但這是一份會被人直接在 Excel／Numbers 開啟的產出物，
一個欄位剛好叫 `=cmd` 就會在開啟時被當成公式執行。這裡對每一格內容做
`_neutralize_formula`：開頭是 `=`／`+`／`-`／`@` 就加一個前導單引號，讓
試算表把它當成純文字讀——單引號在 CSV／試算表慣例裡本來就是「強制文字」
的訊號，不影響資料本身的可讀性。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from erp_migrai.mapping.models import ConflictKind, Mapping, TransformKind

SCHEMA_PATH = Path(__file__).parent / "schema" / "mapping.schema.json"

_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)

# mapping.csv 的欄位順序，逐字照 task-7-brief.md 的規格，不得調整。
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

# `reasons` 欄位裡把多筆理由串起來的分隔符：用全形分號，避免跟 CSV 本身的
# 半形逗號打架（雖然 csv 模組本來就會正確加引號跳脫逗號，但全形分號讓人在
# 試算表裡用眼睛掃過去就能分辨「這是好幾筆理由」，不必依賴引號有沒有顯示）。
_REASONS_JOIN = "；"

# `MappingConflict.kind == ConflictKind.TABLE` 時 `column` 是 `None`——這是
# 整張表對不上模型，不對應任何單一來源欄位。CSV 裡仍然要有一列讓人看到「這張
# 表整個沒對上」，`source_column` 用這個固定字面值顯示，不留空、也不用空字串
# ——留空或空字串會讓人誤以為這是一個名稱剛好是空字串的欄位級衝突，這個字面值
# 讓表級衝突在 CSV 裡一眼可辨識，且與 `engine.py` 既有的
# `conflict.column or "(整張表)"` 訊息慣例一致。
TABLE_CONFLICT_COLUMN_LABEL = "(整張表)"

_TIER_UNMAPPED = "unmapped"
_TIER_CONFLICT = "conflict"

# CSV 公式注入：試算表軟體會把儲存格開頭是這幾個字元的內容當成公式解讀。
_FORMULA_PREFIXES = ("=", "+", "-", "@")


class MappingSerializationError(Exception):
    """`mapping` 內含 `json.dumps` 無法序列化的值。

    正常情況下這不該發生：`rules_loader._ensure_json_serializable` 已經在
    載入規則檔的當下攔住不可序列化的 `transform.value`／`map`／`default`。
    這裡是第二道防線（縱深防禦），只有在呼叫端繞過 rules_loader 直接組出
    `Mapping`（例如測試、或未來的其他呼叫端）時才會走到，讓這種情況也有
    清楚的中文訊息可看，而不是一段 traceback。

    最終整分支審查修正 Important 3：先前這裡只接住 `TypeError`，留了兩個洞：
    (a) `json.dumps` 預設 `allow_nan=True`，會把 Python 的 `float("nan")`／
    `float("inf")` 靜靜寫成 `mapping.json` 裡的 `NaN`／`Infinity`——這兩個
    字面值不是合法 JSON（RFC 8259），會被 `JSON.parse`、Go 的
    `encoding/json`、甚至 Python 自己嚴格模式的解析器拒絕，是本專案最忌諱
    的「悄悄產出錯誤結果」；(b) 循環參照（YAML anchor 如 `value: &a [*a]`
    完全可能產生）會讓 `json.dumps` 拋 `ValueError`，不是 `TypeError`，先前
    完全沒接住，逃出成使用者看到的一長串 traceback。這裡把序列化呼叫改成
    `allow_nan=False`（讓 (a) 直接在這裡變成可控的例外，而不是靜默寫出非法
    JSON），並同時接住 `(TypeError, ValueError)`，兩種輸入都得到同一種清楚
    的中文訊息與 exit 1，不再有 traceback。
    """


class MappingSchemaError(Exception):
    """本管線自己產出的 `mapping` 不符合 `mapping.schema.json`（最終整分支
    審查修正 Important 4）。

    這是映射引擎（或呼叫端組出的 `Mapping`）本身的缺陷，不是使用者
    `--inventory` 的問題——例如引擎算出 `confidence=101`。跟
    `jsonschema.ValidationError` 刻意用不同的 Python 型別，是為了讓
    `erp_migrai.mapping.pipeline.run_map` 的呼叫端（`cli.py`）不必靠例外
    訊息的文字內容去猜「這是使用者輸入問題，還是我們自己的 bug」，能直接
    用型別分開接住，給出正確歸咎對象的訊息。
    """


def validate_mapping(data: dict) -> None:
    """不符合 `mapping.schema.json` 時拋出 `jsonschema.ValidationError`。"""
    _VALIDATOR.validate(data)


def write_mapping(mapping: Mapping, out_dir: Path) -> Path:
    """寫出 `mapping.json` 並回傳其路徑。輸出是確定性的。

    驗證先於任何檔案系統操作：`out_dir` 不存在時，只有在驗證（`validate_
    mapping`）與序列化（`json.dumps`）都通過之後才會被建立
    （`mkdir(parents=True, exist_ok=True)`）；兩者任一失敗，`out_dir` 若原本
    不存在，就仍然不存在，`mapping.json` 更是完全不會被寫出——半成品的輸出
    比沒有輸出更危險。
    """
    payload_dict = mapping.to_dict()
    try:
        validate_mapping(payload_dict)
    except ValidationError as exc:
        raise MappingSchemaError(
            f"映射結果本身不符合 mapping.schema.json（這是映射引擎的缺陷，不是你的 "
            f"--inventory 有問題，請回報 issue）：{exc.message}"
        ) from exc
    try:
        # allow_nan=False：見 MappingSerializationError 的說明——不能讓
        # NaN／Infinity 靜靜寫進這份公開格式，寧可在這裡就變成可控的例外。
        payload = json.dumps(payload_dict, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise MappingSerializationError(f"mapping 內含無法序列化成 JSON 的值：{exc}") from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "mapping.json"
    path.write_text(payload, encoding="utf-8")
    return path


def _transform_cell_and_extra_reasons(transform) -> tuple[str, list[str]]:
    """把一筆 `Transform` 換成 `(transform 欄位內容, 要併入 reasons 的額外說明)`。

    `identity` 是預設情況，不需要額外說明，`transform` 欄位留空。其餘寫
    `kind` 的字面值；`value_map` 額外把整份對照表（與 `default`，若有）寫進
    `reasons`——`transform` 欄位只有一個儲存格的空間，放不下一整份對照表，
    但那份對照表正是人工審核 `value_map` 這筆映射時最需要看到的內容。

    最終整分支審查修正：`constant` 先前只寫 `kind`，實際的常數值完全不在
    這份給人審核的 CSV 裡出現——而「這個常數究竟是什麼」正是審核 `constant`
    這筆映射時唯一需要確認的事。跟 `value_map` 一樣，把值另外説明在
    `reasons`（而不是塞進只有一格空間的 `transform` 欄位），寫法也刻意跟
    `value_map` 對稱。

    `reference`（schema 1.1 新增）：這一欄是外鍵，寫法跟 `constant`／
    `value_map` 對稱——`transform` 欄位只放 `kind` 的字面值，實際參照的
    來源表與欄位另外說明在 `reasons`，讓人在試算表裡一眼看出「這一欄是
    外鍵，指到哪裡」，不必另外開 mapping.json 才看得懂。
    """
    if transform.kind == TransformKind.IDENTITY:
        return "", []

    if transform.kind == TransformKind.VALUE_MAP:
        note = f"轉換方式 value_map，對照表：{json.dumps(transform.value_map, ensure_ascii=False)}"
        if transform.default is not None:
            note += f"；找不到對照時的預設值：{json.dumps(transform.default, ensure_ascii=False)}"
        return transform.kind.value, [note]

    if transform.kind == TransformKind.REFERENCE:
        note = f"轉換方式 reference，參照來源：{transform.ref_table}.{transform.ref_column}"
        return transform.kind.value, [note]

    # 只剩 CONSTANT：把實際的值寫進 reasons，讓人不用另外開 mapping.json
    # 就能看懂「這個常數是什麼」。
    note = f"轉換方式 constant，值：{json.dumps(transform.value, ensure_ascii=False)}"
    return transform.kind.value, [note]


def _neutralize_formula(cell: str) -> str:
    """CSV 公式注入防護：儲存格內容若以 `=`／`+`／`-`／`@` 開頭，加一個前導
    單引號讓試算表軟體把它當純文字讀，而不是當公式執行。

    來源是客戶自己的 DDL，風險本來就低，但 mapping.csv 是明確設計給人在
    試算表裡開啟審核的產出物，跑在不熟悉的客戶環境上，這一步不能省。
    """
    if cell.startswith(_FORMULA_PREFIXES):
        return f"'{cell}"
    return cell


def _field_mapping_row(fm) -> list[str]:
    transform_cell, extra_reasons = _transform_cell_and_extra_reasons(fm.transform)
    reasons = list(fm.reasons) + extra_reasons
    return [
        fm.source.table,
        fm.source.column,
        fm.source.type,
        fm.target.model,
        fm.target.field,
        fm.target.type,
        transform_cell,
        str(fm.confidence),
        str(fm.tier),
        _REASONS_JOIN.join(reasons),
    ]


def _unmapped_row(u) -> list[str]:
    return [
        u.table,
        u.column,
        u.type,
        "",
        "",
        "",
        "",
        "",
        _TIER_UNMAPPED,
        u.reason,
    ]


def _conflict_row(c) -> list[str]:
    column = c.column if c.kind == ConflictKind.COLUMN else TABLE_CONFLICT_COLUMN_LABEL
    reasons = [c.reason]
    if c.candidates:
        reasons.append(f"並列候選：{'、'.join(c.candidates)}")
    return [
        c.table,
        column,
        "",
        "",
        "",
        "",
        "",
        "",
        _TIER_CONFLICT,
        _REASONS_JOIN.join(reasons),
    ]


def write_mapping_csv(mapping: Mapping, out_dir: Path) -> Path:
    """寫出 `mapping.csv` 並回傳其路徑。

    `mappings`／`unmapped`／`conflicts` 三個清單合併成一張表：每一個曾經
    出現在 `Mapping` 裡的來源欄位（或表級衝突）恰好對應一列，讓人在試算表
    裡看到的是**全部**來源欄位，而不是只有對上的那些。

    列的順序不依賴這三個清單串接的先後——那只是引擎內部的分組方式，不是給
    人看的順序——而是對整列內容排序，確保同樣的 `Mapping` 兩次寫出的結果
    逐位元組相同，且同一張表的所有列會排在一起，方便人在試算表裡閱讀。
    """
    rows = (
        [_field_mapping_row(fm) for fm in mapping.mappings]
        + [_unmapped_row(u) for u in mapping.unmapped]
        + [_conflict_row(c) for c in mapping.conflicts]
    )
    # 排序用原始內容（加前導單引號會改變字典序，先排序才能維持「排序只取決
    # 於資料本身」的既有承諾），公式注入防護留到排序之後、寫檔之前才套用。
    rows.sort()
    rows = [[_neutralize_formula(cell) for cell in row] for row in rows]

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "mapping.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)
    return path
