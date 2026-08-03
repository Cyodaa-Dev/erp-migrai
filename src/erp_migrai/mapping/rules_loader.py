"""載入並驗證映射規則檔（`rules/*.yaml`／`rules/*.yml`）。

規則檔是**貢獻點③**：外部貢獻者要讓 migrai 認得自己環境裡的欄位命名，
只需要在 `rules/` 底下加一個 YAML 檔案，不必碰任何 Python（見
`rules/README.md`）。命中規則的欄位對應，其可信度（confidence）與分層
（tier）由呼叫端（Task 6 的引擎）直接判為 100／`auto`——因為規則就是人明
確教過的事實，不是猜測。

這個模組只做「讀檔、驗證、轉成 dataclass」，跟 `catalog.py` 對 catalog 的
定位完全對稱。驗證需要 catalog 在手：規則的 `target` 指到 catalog 裡不
存在的模型或欄位，是**規則的錯**，必須在載入當下就失敗，絕不能默默忽略
——那正是本專案存在的理由（悄悄產生錯誤的結果）。

YAML 解析經由 `erp_migrai.mapping.yamlio.safe_load_strict`（與 catalog.py
共用同一份「重複 key 直接丟例外」的加嚴邏輯），理由與 catalog.py 相同：
複製貼上一條規則、忘記改鍵名，不能讓前一條規則悄悄消失。

`rules_dir` 底下同時接受 `*.yaml` 與 `*.yml`（最終整分支審查修正：一位
照著 `rules/README.md` 的說明去做的貢獻者，若把檔案存成 `.yml`，先前只
掃 `*.yaml` 會讓這個檔案被整份忽略、且完全不出聲——覆蓋率悄悄變成 0%，
使用者卻拿到 exit 0）；兩者合併後一律依**檔名排序**才讀取，確保兩個檔案
之間若有衝突，錯誤訊息（點名哪兩個 origin）在每台機器上都完全一樣。
`rules_dir` 底下除了 `*.yaml`／`*.yml` 與 `README.md` 之外的其他檔案不會
被讀取，但會被記在 `RuleSet.ignored_files`，讓呼叫端（CLI）能回報「這個
目錄裡有一個我沒讀的檔案」，而不是讓它徹底隱形。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from erp_migrai.mapping.catalog import OdooModel
from erp_migrai.mapping.models import Transform, TransformKind
from erp_migrai.mapping.yamlio import safe_load_strict

# 目前唯一支援的規則檔版本。往後若規則檔格式需要不相容的變動，就調高這個
# 數字並在這裡加上對應的相容性處理——現在先大聲拒絕看不懂的版本。
_SUPPORTED_VERSION = 1

_DEFAULT_RULES_DIR = Path(__file__).with_name("rules")

_ALLOWED_TRANSFORM_KINDS = {kind.value for kind in TransformKind}


class RuleError(Exception):
    """規則檔本身的結構、內容，或與 catalog 的一致性有問題。

    每個訊息都必須點名是哪個檔案的哪個鍵出的錯（例如
    `res_partner.yaml 的 cust_mast.cust_stus`），讓貢獻者不必逐行比對整份
    YAML、也不必回頭查 catalog，就能直接定位問題所在。
    """


@dataclass
class ColumnRule:
    """一條「來源欄位 → Odoo 欄位」的規則。"""

    source_table: str
    source_column: str
    target_model: str
    target_field: str
    transform: Transform
    # 哪個檔案的哪個鍵定義了這條規則，例如
    # `res_partner.yaml#cust_mast.cust_no`，會被放進 Mapping 的 reasons。
    origin: str


@dataclass
class TableRule:
    """一條「來源資料表 → Odoo 模型」的規則。"""

    source_table: str
    target_model: str
    origin: str


# `rules_dir` 底下不算「被忽略」的檔案：這是文件本身，不是規則，也不是
# 「貢獻者可能打錯副檔名」的候選。
_NON_RULE_FILENAMES = frozenset({"README.md"})


@dataclass
class RuleSet:
    """`rules/` 底下所有規則檔合併後的結果。"""

    table_rules: dict[str, TableRule]
    column_rules: dict[tuple[str, str], ColumnRule]
    # 目錄裡存在、但沒被當成規則檔讀取的檔案名稱（`README.md` 除外），依
    # 檔名排序。空清單代表目錄很乾淨；非空清單交給 CLI 回報，讓「有個檔案
    # 我沒讀」這件事對使用者可見，而不是徹底隱形（最終整分支審查修正）。
    ignored_files: list[str] = field(default_factory=list)


def load_rules(catalog: dict[str, OdooModel], rules_dir: Path | None = None) -> RuleSet:
    """載入並驗證 `rules_dir`（預設為套件內建的 `rules/`）底下所有規則檔。

    需要傳入已經載入好的 `catalog`：唯有如此才能驗證每條規則的 `target`
    真的指到 catalog 裡存在的模型與欄位——這正是本模組「不讓規則默默覆蓋
    掉一個不存在的目標」的手段。

    空目錄（沒有任何 `*.yaml`／`*.yml`）回傳空的 `RuleSet`，不算錯誤：沒有
    規則本來就是合法狀態，不是每個 T100 安裝都需要自訂規則。
    """
    target_dir = rules_dir if rules_dir is not None else _DEFAULT_RULES_DIR

    if not target_dir.is_dir():
        raise RuleError(f"規則目錄 {target_dir} 不存在或不是目錄")

    table_rules: dict[str, TableRule] = {}
    column_rules: dict[tuple[str, str], ColumnRule] = {}

    rule_paths = sorted(
        {*target_dir.glob("*.yaml"), *target_dir.glob("*.yml")}, key=lambda p: p.name
    )
    for path in rule_paths:
        _load_rule_file(path, catalog, table_rules, column_rules)

    rule_filenames = {path.name for path in rule_paths}
    ignored_files = sorted(
        entry.name
        for entry in target_dir.iterdir()
        if entry.is_file()
        and entry.name not in rule_filenames
        and entry.name not in _NON_RULE_FILENAMES
        # Minor 5（最終整分支審查修正）：`.DS_Store`（macOS）、`.gitkeep` 這類
        # 點開頭的檔案是作業系統／版本控制的產物，不是「貢獻者把副檔名打錯字」
        # 的候選——這本專案是在 macOS 上開發，`rules/` 目錄下出現 `.DS_Store`
        # 是常態，不該被回報成「這通常代表副檔名打錯字」而讓使用者白跑一趟。
        and not entry.name.startswith(".")
    )

    return RuleSet(table_rules=table_rules, column_rules=column_rules, ignored_files=ignored_files)


def _load_rule_file(
    path: Path,
    catalog: dict[str, OdooModel],
    table_rules: dict[str, TableRule],
    column_rules: dict[tuple[str, str], ColumnRule],
) -> None:
    file_label = path.name

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleError(f"無法讀取規則檔 {path}：{exc}") from exc

    try:
        raw = safe_load_strict(text, str(path))
    except yaml.YAMLError as exc:
        raise RuleError(f"{file_label} 不是合法的 YAML：{exc}") from exc

    if not isinstance(raw, dict):
        raise RuleError(
            f"{file_label} 頂層必須是 mapping（含 version／tables／columns），"
            f"實際讀到 {type(raw).__name__}"
        )

    version = raw.get("version")
    if version != _SUPPORTED_VERSION:
        raise RuleError(
            f"{file_label} 的 `version` 為 {version!r}，目前只支援 {_SUPPORTED_VERSION}"
        )

    tables_raw = raw.get("tables", {})
    if not isinstance(tables_raw, dict):
        raise RuleError(
            f"{file_label} 的 `tables` 必須是 mapping，實際為 {type(tables_raw).__name__}"
        )
    for source_table, body in tables_raw.items():
        rule = _parse_table_rule(file_label, source_table, body, catalog)
        _merge_table_rule(table_rules, rule)

    columns_raw = raw.get("columns", {})
    if not isinstance(columns_raw, dict):
        raise RuleError(
            f"{file_label} 的 `columns` 必須是 mapping，實際為 {type(columns_raw).__name__}"
        )
    for key, body in columns_raw.items():
        rule = _parse_column_rule(file_label, key, body, catalog)
        _merge_column_rule(column_rules, rule)


def _parse_table_rule(
    file_label: str, source_table: str, body: Any, catalog: dict[str, OdooModel]
) -> TableRule:
    where = f"{file_label} 的 `{source_table}`"

    if not isinstance(body, dict):
        raise RuleError(f"{where} 的定義必須是 mapping，實際為 {type(body).__name__}")

    target_model = body.get("target_model")
    if not isinstance(target_model, str) or not target_model.strip():
        raise RuleError(f"{where} 缺少非空的 `target_model`")

    if target_model not in catalog:
        raise RuleError(f"{where} 的 `target_model` 指到 catalog 沒有的模型 `{target_model}`")

    origin = f"{file_label}#{source_table}"
    return TableRule(source_table=source_table, target_model=target_model, origin=origin)


def _parse_column_rule(
    file_label: str, key: Any, body: Any, catalog: dict[str, OdooModel]
) -> ColumnRule:
    where = f"{file_label} 的 `{key}`"

    if not isinstance(key, str) or "." not in key:
        raise RuleError(f"{where} 不是合法的規則鍵——必須是 `來源資料表.來源欄位` 的形式")
    source_table, _, source_column = key.partition(".")
    if not source_table or not source_column:
        raise RuleError(f"{where} 不是合法的規則鍵——必須是 `來源資料表.來源欄位` 的形式")

    if not isinstance(body, dict):
        raise RuleError(f"{where} 的定義必須是 mapping，實際為 {type(body).__name__}")

    target = body.get("target")
    if not isinstance(target, str) or "." not in target:
        raise RuleError(f"{where} 的 `target` 不是 `模型.欄位` 的形式（實際為 {target!r}）")
    target_model, _, target_field = target.rpartition(".")
    if not target_model or not target_field:
        raise RuleError(f"{where} 的 `target` 不是 `模型.欄位` 的形式（實際為 {target!r}）")

    model = catalog.get(target_model)
    if model is None:
        raise RuleError(f"{where} 的 `target` 指到 catalog 沒有的模型 `{target_model}`")
    if target_field not in model.fields:
        raise RuleError(
            f"{where} 的 `target` 指到模型 `{target_model}` 沒有的欄位 `{target_field}`"
        )

    transform = _parse_transform(where, body.get("transform"))

    origin = f"{file_label}#{key}"
    return ColumnRule(
        source_table=source_table,
        source_column=source_column,
        target_model=target_model,
        target_field=target_field,
        transform=transform,
        origin=origin,
    )


def _ensure_json_serializable(where: str, label: str, value: Any) -> None:
    """確認 `value` 能被 `json.dumps` 序列化，否則丟 `RuleError`。

    最終整分支審查修正：`mapping.schema.json` 的 `transform.value` 刻意
    宣告成 `{}`（接受任何型別），跨欄位的「必須能寫進 JSON」這條規則本來
    就交給這裡檢查（見 schema 本身的 description）。問題是這裡先前完全沒
    做這件事——YAML 1.1 會把沒加引號的日期（`2024-01-01`）解析成
    `datetime.date`、把某些數字字面值解析成非預期型別，這種值一路帶進
    `Transform`、直到 `writer.py` 呼叫 `json.dumps` 才會爆炸成一個
    `TypeError`，變成使用者看到的一長串 traceback。在載入規則檔的當下
    就攔下來，才能點名是哪個檔案的哪個鍵、给出貢獻者能直接動手修的建議
    （加引號），而不是讓錯誤在完全不同的階段、以完全看不懂的方式冒出來。

    最終整分支審查修正 Important 3：先前這裡只接住 `TypeError`，留了兩個洞：

    (a) `json.dumps` 預設 `allow_nan=True`，YAML 的 `.nan`／`.inf` 會被
        `safe_load_strict` 解析成 Python 的 `float("nan")`／`float("inf")`，
        這裡的檢查因此完全放行——一路帶到 `writer.py` 才會把 `NaN` 這個
        不合法 JSON（RFC 8259）的字面值寫進公開的 `mapping.json`。這裡改用
        `allow_nan=False`，讓這種值在載入規則檔的當下就被攔下來，而不是等
        寫檔那一刻才被（原本也沒接住的）另一道防線抓到。
    (b) YAML anchor 可以寫出循環參照（例如 `value: &a [*a]`），
        `json.dumps` 對循環參照拋的是 `ValueError`，不是 `TypeError`，先前
        完全沒接住，會讓整個例外原樣逃出，變成使用者看到的一長串
        traceback。這裡一併接住 `(TypeError, ValueError)`。
    """
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuleError(
            f"{where} 的 `{label}` 含有無法寫進 mapping.json 的值（{exc}）——"
            "YAML 常見的原因是沒加引號的日期或特殊數字字面值被解析成非純量的 "
            "Python 物件（例如 `2024-01-01` 會變成 datetime.date）、"
            "`.nan`／`.inf` 這種不是合法 JSON 的特殊浮點數字面值，"
            "或是用 YAML anchor 寫出的循環參照；"
            "把這個值加上引號寫成字串、或移除循環參照通常就能修正"
        ) from exc


def _parse_transform(where: str, body: Any) -> Transform:
    if body is None:
        return Transform(kind=TransformKind.IDENTITY)

    if not isinstance(body, dict):
        raise RuleError(f"{where} 的 `transform` 必須是 mapping，實際為 {type(body).__name__}")

    kind = body.get("kind")
    if kind not in _ALLOWED_TRANSFORM_KINDS:
        allowed = ", ".join(sorted(_ALLOWED_TRANSFORM_KINDS))
        raise RuleError(
            f"{where} 的 `transform.kind` 為 `{kind}`，不在允許集合內（允許：{allowed}）"
        )

    if kind == TransformKind.IDENTITY:
        return Transform(kind=TransformKind.IDENTITY)

    if kind == TransformKind.VALUE_MAP:
        value_map = body.get("map")
        if not isinstance(value_map, dict) or not value_map:
            raise RuleError(f"{where} 的 `transform.kind` 是 `value_map`，但缺少非空的 `map`")
        default = body.get("default")
        _ensure_json_serializable(where, "map", value_map)
        _ensure_json_serializable(where, "default", default)
        return Transform(
            kind=TransformKind.VALUE_MAP,
            value_map=value_map,
            default=default,
        )

    if kind == TransformKind.REFERENCE:
        ref_table = body.get("ref_table")
        if not isinstance(ref_table, str) or not ref_table.strip():
            raise RuleError(f"{where} 的 `transform.kind` 是 `reference`，但缺少非空的 `ref_table`")
        ref_column = body.get("ref_column")
        if not isinstance(ref_column, str) or not ref_column.strip():
            raise RuleError(
                f"{where} 的 `transform.kind` 是 `reference`，但缺少非空的 `ref_column`"
            )
        return Transform(
            kind=TransformKind.REFERENCE,
            ref_table=ref_table,
            ref_column=ref_column,
        )

    # 只剩 CONSTANT：`value` 必須存在（哪怕值是 false／0），
    # 用 `in` 而非真值判斷，避免把「合法填了假值」誤判成「沒填」。
    if "value" not in body:
        raise RuleError(f"{where} 的 `transform.kind` 是 `constant`，但缺少 `value`")
    value = body["value"]
    # `value: null` 例外——YAML 的 `value:` 後面留空就是 None，很容易不小心打出來，
    # 而 `constant` 配 `None` 是結構壞掉：規則作者若真的想寫常數空字串，會寫
    # `value: ""`。載入階段的 `apply_transform` 已經會拒絕這個形狀，但那太晚了：
    # `migrai map` 會乾淨地通過（exit 0、mapping.json 寫出、mapping.csv 可審），
    # 使用者一路走到 `migrai load` 才在那一欄的每一列吃到 E-4002。能在更早一層
    # 判定就在更早一層大聲失敗，兩層的判定必須一致。
    if value is None:
        raise RuleError(
            f"{where} 的 `transform.kind` 是 `constant`，但 `value` 是空的"
            f'——若要寫入空字串請明確設定 `value: ""`'
        )
    _ensure_json_serializable(where, "value", value)
    return Transform(kind=TransformKind.CONSTANT, value=value)


def _merge_table_rule(table_rules: dict[str, TableRule], rule: TableRule) -> None:
    existing = table_rules.get(rule.source_table)
    if existing is None:
        table_rules[rule.source_table] = rule
        return
    if existing.target_model == rule.target_model:
        return  # 重複但無矛盾：保留先讀到（排序在前）的那一份。
    raise RuleError(
        f"來源資料表 `{rule.source_table}` 被兩條規則指到不同的 target_model："
        f"{existing.origin} 指到 `{existing.target_model}`，"
        f"{rule.origin} 指到 `{rule.target_model}`"
    )


def _merge_column_rule(column_rules: dict[tuple[str, str], ColumnRule], rule: ColumnRule) -> None:
    key = (rule.source_table, rule.source_column)
    existing = column_rules.get(key)
    if existing is None:
        column_rules[key] = rule
        return
    if (existing.target_model, existing.target_field) == (rule.target_model, rule.target_field):
        return  # 重複但無矛盾：保留先讀到（排序在前）的那一份。
    raise RuleError(
        f"來源欄位 `{rule.source_table}.{rule.source_column}` 被兩條規則指到不同的 target："
        f"{existing.origin} 指到 `{existing.target_model}.{existing.target_field}`，"
        f"{rule.origin} 指到 `{rule.target_model}.{rule.target_field}`"
    )
