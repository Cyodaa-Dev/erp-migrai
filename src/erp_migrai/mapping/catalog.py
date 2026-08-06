"""載入並驗證 Odoo 目標欄位 catalog（預設 `odoo19_catalog.yaml`）。

這個模組只做三件事：讀檔、驗證結構、轉成 dataclass。**不含任何映射邏輯**
——要不要把某個來源欄位對到某個目標欄位，是 `map` 其他模組的事，這裡只負責
回答「目標 Odoo 上這個模型、這個欄位，長什麼樣子」。

預設 catalog（`odoo19_catalog.yaml`）是 `tools/introspect_catalog.py` 對一台
真實 Odoo 執行 `fields_get` 產生的；同目錄的 `odoo17_catalog.yaml` 則是早期
**人工整理**的版本（檔頭自己聲明可能有錯，事實上 `res.partner.name` 的
`required` 就記錯了）。不論哪一種來源都可能與目標環境不符——欄位所屬模組
沒安裝、版本之間欄位被移除或改型別。這個驗證器存在的理由，就是不讓一份錯的 catalog 悄悄產出
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

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from erp_migrai.mapping.yamlio import safe_load_strict

# 允許的目標欄位型別，封閉集合。Task 5 的型別相容表是照著這份清單的名稱寫的，
# 所以不能隨意擴充；catalog 裡出現任何不在這裡面的型別都是錯誤，不是待補的
# 新型別。
#
# 要新增一個型別，必須同時在 `typing_rules._COMPATIBILITY` 的**每一個**來源
# 家族補上對應的一格——那張表刻意窮舉，沒有 fall-through，漏一格會被
# `tests/mapping/test_typing_rules.py` 的完整性測試抓到。
ALLOWED_FIELD_TYPES: frozenset[str] = frozenset(
    {
        "char",
        "text",
        # Odoo 的 html 欄位（`res.partner.comment`、`product.template.description`、
        # `sale.order.note` 在 Odoo 19 都是 html；同名欄位在 Odoo 17 是 text）。
        # 純文字寫進 html 欄位不會報錯，但換行不會呈現、`<` 與 `&` 會被當標記
        # 解讀——這是靜默損壞，相容表因此不給滿分，見 typing_rules。
        "html",
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

# 預設 catalog ＝ Odoo 19（由 `tools/introspect_catalog.py` 對一台真實實例執行
# `fields_get` 產生，不是人工抄寫）。要對別的版本跑，用環境變數指到別的
# catalog 檔，例如仍在 Odoo 17 的環境：
#     ERP_MIGRAI_CATALOG=.../odoo17_catalog.yaml migrai map ...
# `odoo17_catalog.yaml` 保留在同一個目錄，沒有移除。
_DEFAULT_CATALOG_PATH = Path(
    os.environ.get("ERP_MIGRAI_CATALOG") or Path(__file__).with_name("odoo19_catalog.yaml")
)


class CatalogError(Exception):
    """catalog 檔案本身的結構或內容有問題。

    每個訊息都必須點名是哪個模型、哪個欄位（或哪個頂層 key）出的錯，讓人
    不必逐行比對整份 YAML 就能定位問題所在。
    """


@dataclass
class OdooField:
    """目標 Odoo 上的一個欄位。"""

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
    """目標 Odoo 上的一個模型與它（在 MVP 範圍內）擁有的欄位。"""

    name: str
    fields: dict[str, OdooField] = field(default_factory=dict)
    description: str = ""


def load_catalog(path: Path | None = None) -> dict[str, OdooModel]:
    """載入並驗證 catalog，回傳 `{model_name: OdooModel}`。

    `path=None` 表示使用預設 catalog（見 `_DEFAULT_CATALOG_PATH`，與 `ddl.py` 定位
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

    # 這裡曾經有一道「模型必須至少有一個 required: true 欄位」的檢查，理由是
    # 「沒有必填欄位的模型幾乎一定是整理漏了」。20260806 對 Odoo 19 真機
    # introspect 之後移除——那個前提在機器讀來的資料上不成立：
    #
    #   - catalog 收的是**策展子集**（我們會映射的欄位），不是模型的全部欄位。
    #     `res.partner` 在 Odoo 19 確實有 required 欄位（autopost_bills、
    #     group_rfq、group_on），但沒有一個在我們的子集裡；`mrp.workcenter`
    #     同理（resource_id、sequence、currency_id）。
    #   - 而且原本手寫的 `odoo17_catalog.yaml` 把 `res.partner.name` 記成
    #     `required: true`，真機 `fields_get` 回的是 **False**（Odoo 用 SQL
    #     constraint 而非欄位層 required 來管它）。舊檢查之所以一直過，是因為
    #     它建立在一筆錯誤記載上。
    #
    # 「子集裡沒有必填欄位」不代表整理漏了，所以不再視為錯誤。

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
