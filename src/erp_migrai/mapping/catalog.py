"""載入並驗證 Odoo 17 目標欄位 catalog（`odoo17_catalog.yaml`）。

這個模組只做三件事：讀檔、驗證結構、轉成 dataclass。**不含任何映射邏輯**
——要不要把某個來源欄位對到某個目標欄位，是 `map` 其他模組的事，這裡只負責
回答「Odoo 17 上這個模型、這個欄位，長什麼樣子」。

catalog 檔案本身是**人工整理**的（見 `odoo17_catalog.yaml` 檔頭），不是從
一台實際的 Odoo 反射出來的，所以它可能有錯——型別記錯、欄位其實不存在、
或 module 掛錯。這個驗證器存在的理由，就是不讓一份錯的 catalog 悄悄產出
一整批看起來成功、其實全錯的映射結果：任何結構或型別上的問題，都必須是
`CatalogError`，而且訊息要點名是哪個模型、哪個欄位——貢獻者才能直接定位，
不必回頭把整份 YAML 手動比對一遍。

YAML 解析一律經由 `yamlio.safe_load_strict`（見 `yamlio.py`）：它在效果上
等同 `yaml.safe_load`——catalog 與 rules 檔都是「人送的資料檔」（例如透過
pull request），不指定安全 Loader 的 `yaml.load` 可以從檔案內容執行任意
Python，這是不能接受的攻擊面——但額外加嚴：同一個 mapping 裡出現重複 key
時會直接丟例外，而不是像 PyYAML 預設那樣悄悄保留最後一次、蓋掉前面的定義。
這份加嚴邏輯與 rules 載入器（`rules_loader.py`）共用，定義只有一份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from erp_migrai.mapping.yamlio import safe_load_strict

# 允許的目標欄位型別，封閉集合。Task 5 的型別相容表是照著這份清單的名稱寫的，
# 所以不能隨意擴充；catalog 裡出現任何不在這裡面的型別都是錯誤，不是待補的
# 新型別。
ALLOWED_FIELD_TYPES: frozenset[str] = frozenset(
    {
        "char",
        "text",
        "integer",
        "float",
        "monetary",
        "boolean",
        "date",
        "datetime",
        "selection",
        "many2one",
        "one2many",
        "many2many",
    }
)

# Odoo 技術模型名稱慣例：小寫字母、數字、底線構成的片段，用 `.` 連接至少
# 兩段（例如 `res.partner`、`sale.order.line`）。片段不能是空的，也不能以
# 數字開頭。
_MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

# 每個欄位定義必須有的四個 key。缺一個都代表整理疏漏，且下游（mapping.csv
# 給人審核用的 help 文字、load 階段要不要求對應模組）都會跟著出錯。
_REQUIRED_FIELD_KEYS = ("type", "required", "module", "help")

_DEFAULT_CATALOG_PATH = Path(__file__).with_name("odoo17_catalog.yaml")


class CatalogError(Exception):
    """catalog 檔案本身的結構或內容有問題。

    每個訊息都必須點名是哪個模型、哪個欄位（或哪個頂層 key）出的錯，讓人
    不必逐行比對整份 YAML 就能定位問題所在。
    """


@dataclass
class OdooField:
    """Odoo 17 上的一個欄位。"""

    name: str
    type: str
    required: bool
    # 欄位所屬的模組（例如 `base`／`account`／`sale`／`product`）。欄位是否
    # 真的存在，取決於這個模組有沒有被安裝——這是這份 catalog 最容易被忽略、
    # 但一旦錯就會在 load 階段炸得莫名其妙的地方，所以每個欄位都必須有它。
    module: str
    # 給人審核 mapping.csv 用的簡短中文說明，讓審核者不必打開 Odoo 也知道
    # 這個欄位的用途。刻意不允許空字串：空的 help 等於沒有這個欄位存在的理由。
    help: str


@dataclass
class OdooModel:
    """Odoo 17 上的一個模型與它（在 MVP 範圍內）擁有的欄位。"""

    name: str
    fields: dict[str, OdooField] = field(default_factory=dict)
    description: str = ""


def load_catalog(path: Path | None = None) -> dict[str, OdooModel]:
    """載入並驗證 catalog，回傳 `{model_name: OdooModel}`。

    `path=None` 表示使用套件內建的 `odoo17_catalog.yaml`（與 `ddl.py` 定位
    `ddl.lark` 的方式相同：相對於本檔案）。傳入自訂路徑時完全不碰內建檔，
    方便測試與（未來）覆寫。

    回傳的 dict 依 YAML 檔案裡模型出現的順序建立——對同一份檔案永遠是同一個
    順序，不依賴 set 或雜湊迭代順序，滿足確定性要求。
    """
    target = path if path is not None else _DEFAULT_CATALOG_PATH

    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"無法讀取 catalog 檔案 {target}：{exc}") from exc

    try:
        raw = safe_load_strict(text, str(target))
    except yaml.YAMLError as exc:
        raise CatalogError(f"{target} 不是合法的 YAML：{exc}") from exc

    if raw is None:
        raise CatalogError(f"{target} 是空檔案，頂層必須是包含 `models` 的 mapping")
    if not isinstance(raw, dict):
        raise CatalogError(
            f"{target} 頂層必須是 mapping（例如 `models: ...`），實際讀到 {type(raw).__name__}"
        )

    models_raw = raw.get("models")
    if not isinstance(models_raw, dict):
        raise CatalogError(
            f"{target} 缺少頂層 `models`，或其型別錯誤"
            f"（必須是 mapping，實際為 {type(models_raw).__name__}）"
        )
    if not models_raw:
        raise CatalogError(f"{target} 的 `models` 是空的")

    return {name: _parse_model(name, body) for name, body in models_raw.items()}


def _parse_model(model_name: str, model_body: Any) -> OdooModel:
    if not _MODEL_NAME_RE.match(model_name):
        raise CatalogError(f"模型名稱 `{model_name}` 不是 Odoo 慣例的 `a.b` 點號分隔形式")
    if not isinstance(model_body, dict):
        raise CatalogError(
            f"模型 `{model_name}` 的定義必須是 mapping，實際為 {type(model_body).__name__}"
        )

    description = model_body.get("description")
    if not isinstance(description, str) or not description.strip():
        raise CatalogError(f"模型 `{model_name}` 缺少非空的 `description`")

    fields_raw = model_body.get("fields")
    if not isinstance(fields_raw, dict):
        raise CatalogError(
            f"模型 `{model_name}` 的 `fields` 必須是 mapping"
            f"（例如逐一縮排的欄位定義），實際為 {type(fields_raw).__name__}"
        )
    if not fields_raw:
        raise CatalogError(f"模型 `{model_name}` 的 `fields` 是空的")

    fields: dict[str, OdooField] = {}
    for field_name, field_body in fields_raw.items():
        fields[field_name] = _parse_field(model_name, field_name, field_body)

    if not any(f.required for f in fields.values()):
        raise CatalogError(
            f"模型 `{model_name}` 沒有任何 `required: true` 的欄位——"
            "沒有必填欄位的模型幾乎一定是整理漏了，請確認"
        )

    return OdooModel(name=model_name, fields=fields, description=description)


def _parse_field(model_name: str, field_name: str, field_body: Any) -> OdooField:
    where = f"{model_name}.{field_name}"

    if not isinstance(field_body, dict):
        raise CatalogError(
            f"欄位 `{where}` 的定義必須是 mapping"
            f"（含 type／required／module／help），實際為 {type(field_body).__name__}"
        )

    for key in _REQUIRED_FIELD_KEYS:
        if key not in field_body:
            raise CatalogError(f"欄位 `{where}` 缺少必要的 `{key}`")

    field_type = field_body["type"]
    if field_type not in ALLOWED_FIELD_TYPES:
        allowed = ", ".join(sorted(ALLOWED_FIELD_TYPES))
        raise CatalogError(
            f"欄位 `{where}` 的 `type` 為 `{field_type}`，不在允許集合內（允許：{allowed}）"
        )

    required = field_body["required"]
    if not isinstance(required, bool):
        raise CatalogError(
            f"欄位 `{where}` 的 `required` 必須是 boolean，實際為 {type(required).__name__}"
        )

    module = field_body["module"]
    if not isinstance(module, str) or not module.strip():
        raise CatalogError(f"欄位 `{where}` 的 `module` 必須是非空字串")

    help_text = field_body["help"]
    if not isinstance(help_text, str) or not help_text.strip():
        raise CatalogError(f"欄位 `{where}` 的 `help` 必須是非空字串")

    return OdooField(
        name=field_name,
        type=field_type,
        required=required,
        module=module,
        help=help_text,
    )
