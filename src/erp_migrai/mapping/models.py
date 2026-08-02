"""mapping.json 的資料模型。

設計規則與 inventory 相同：**確定性**——不含時間戳、絕對路徑、UUID 或任何
隨機值。同樣輸入必須產生逐位元組相同的 JSON。

這是對下游（含商業版 consumer）承諾的公開格式，`schema_version` 是相容性契約。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "1.1"

# 三級可信度分層的邊界，照 spec §3.1。
AUTO_THRESHOLD = 90
SUGGESTED_THRESHOLD = 70


class Tier(StrEnum):
    AUTO = "auto"
    SUGGESTED = "suggested"
    NEEDS_HUMAN = "needs_human"


class TransformKind(StrEnum):
    IDENTITY = "identity"
    VALUE_MAP = "value_map"
    CONSTANT = "constant"
    REFERENCE = "reference"


class ConflictKind(StrEnum):
    """`MappingConflict` 的判別欄位：這筆衝突是整張表對不上模型，還是某張表
    已經對到模型、但某個欄位對不上目標欄位。修正輪次 1（Important 3，人類
    裁定）新增：先前引擎用 `column == ""` 當表級衝突的哨兵值，但
    `mapping.schema.json` 把 `column` 宣告成一般字串，遵循契約的消費者會把
    表級衝突誤讀成「有一個名字剛好叫空字串的欄位衝突」；`candidates` 的意義
    也隨 `kind` 而不同（見 `MappingConflict` docstring）。"""

    TABLE = "table"
    COLUMN = "column"


def tier_for(confidence: int) -> Tier:
    """把 0-100 的可信度換成分層。超出範圍代表計算有誤，大聲失敗。"""
    if not 0 <= confidence <= 100:
        raise ValueError(f"可信度必須是 0 到 100 的整數，收到 {confidence}")
    if confidence >= AUTO_THRESHOLD:
        return Tier.AUTO
    if confidence >= SUGGESTED_THRESHOLD:
        return Tier.SUGGESTED
    return Tier.NEEDS_HUMAN


@dataclass
class Transform:
    """值的轉換方式。有四種：`identity`（原封不動搬過去）、`value_map`
    （離散值對照）、`constant`（固定值）、`reference`（外鍵：這個來源欄位
    的值要解析成 `ref_table` 這張來源表裡、以 `ref_column` 為主鍵的某一筆
    記錄，再換成該記錄在 Odoo 裡的 id；實際解析是載入階段的工作，這裡只
    承載「參照哪張表的哪個欄位」這個事實）。細節與 YAML 寫法見
    `src/erp_migrai/mapping/rules/README.md` 的「transform」一節。"""

    kind: TransformKind
    value_map: dict[str, Any] | None = None
    default: Any = None
    value: Any = None
    # 只有 kind == REFERENCE 會用到；其餘 kind 一律是 None。
    ref_table: str | None = None
    ref_column: str | None = None


@dataclass
class SourceRef:
    table: str
    column: str
    type: str


@dataclass
class TargetRef:
    model: str
    field: str
    type: str


@dataclass
class FieldMapping:
    source: SourceRef
    target: TargetRef
    transform: Transform
    confidence: int
    tier: Tier
    # 為什麼是這個分數。這是人工審核的唯一依據，不得為空。
    reasons: list[str] = field(default_factory=list)


@dataclass
class UnmappedColumn:
    """來源有、對不上目標的欄位。這是盤點結果，不是錯誤清單。"""

    table: str
    column: str
    type: str
    reason: str


@dataclass
class MappingConflict:
    """`kind` 決定 `column`／`candidates` 的意義：

    - `kind == ConflictKind.TABLE`：整張表對不上模型（或對到多個並列的模型）。
      `column` 為 `None`（這筆衝突不對應任何單一來源欄位），`candidates` 是
      互相並列的 **Odoo 模型名稱**清單。
    - `kind == ConflictKind.COLUMN`：表已經對到模型，但某個欄位對不上目標
      欄位（或對到多個並列的欄位／規則衝突）。`column` 是實際的來源欄位
      名稱，`candidates` 是 `模型.欄位` 字串清單。
    """

    table: str
    column: str | None
    kind: ConflictKind
    candidates: list[str]
    reason: str


@dataclass
class ModelMapping:
    source_table: str
    target_model: str
    confidence: int
    tier: Tier
    reasons: list[str] = field(default_factory=list)


@dataclass
class MappingStats:
    source_columns: int
    mapped_columns: int
    coverage_percent: int
    auto: int
    suggested: int
    needs_human: int


@dataclass
class Mapping:
    schema_version: str
    # 上游 inventory 是否為合成語料。誠實性資訊要一路傳下去，不在中途消失。
    inventory_synthetic: bool
    # stats 刻意沒有預設值：schema 把它列為 required，若允許 None 就會出現
    # 「建構得出來但驗證不過」的物件，那是等著有人踩的坑。
    stats: MappingStats
    models: list[ModelMapping] = field(default_factory=list)
    mappings: list[FieldMapping] = field(default_factory=list)
    unmapped: list[UnmappedColumn] = field(default_factory=list)
    conflicts: list[MappingConflict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
