"""rules_loader.load_rules 的測試。

涵蓋兩類案例：內建 `rules/` 的內容正確性，以及驗證器對各種畸形規則檔的
「大聲失敗」行為——一條規則的 `target` 指到 catalog 不存在的模型或欄位，
必須在載入時就失敗，不能默默被忽略（那正是這個任務存在的理由）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from erp_migrai.mapping.catalog import load_catalog
from erp_migrai.mapping.models import TransformKind
from erp_migrai.mapping.rules_loader import (
    ColumnRule,
    RuleError,
    RuleSet,
    TableRule,
    load_rules,
)

CATALOG = load_catalog()


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 內建 rules/ 的內容
# ---------------------------------------------------------------------------


def test_builtin_rules_load_cust_mast_table_rule_and_seven_column_rules():
    rules = load_rules(CATALOG)
    assert isinstance(rules, RuleSet)

    assert "cust_mast" in rules.table_rules
    table_rule = rules.table_rules["cust_mast"]
    assert isinstance(table_rule, TableRule)
    assert table_rule.source_table == "cust_mast"
    assert table_rule.target_model == "res.partner"

    cust_mast_columns = {
        key: rule for key, rule in rules.column_rules.items() if key[0] == "cust_mast"
    }
    assert len(cust_mast_columns) == 7


def test_cust_stus_rule_has_value_map_transform():
    rules = load_rules(CATALOG)
    rule = rules.column_rules[("cust_mast", "cust_stus")]
    assert isinstance(rule, ColumnRule)
    assert rule.target_model == "res.partner"
    assert rule.target_field == "active"
    assert rule.transform.kind == TransformKind.VALUE_MAP
    assert rule.transform.value_map == {"Y": True, "N": False}
    assert rule.transform.default is True


def test_rule_without_transform_key_gets_identity():
    rules = load_rules(CATALOG)
    rule = rules.column_rules[("cust_mast", "cust_no")]
    assert rule.transform.kind == TransformKind.IDENTITY


def test_origin_names_file_and_key():
    rules = load_rules(CATALOG)
    rule = rules.column_rules[("cust_mast", "cust_no")]
    assert rule.origin == "res_partner.yaml#cust_mast.cust_no"


def test_custom_rules_dir_is_read_instead_of_builtin(tmp_path: Path):
    _write(
        tmp_path,
        "custom.yaml",
        """
version: 1
tables:
  my_tbl:
    target_model: res.partner
columns:
  my_tbl.my_col:
    target: res.partner.ref
""",
    )
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    assert set(rules.table_rules) == {"my_tbl"}
    assert set(rules.column_rules) == {("my_tbl", "my_col")}


def test_empty_rules_dir_returns_empty_ruleset(tmp_path: Path):
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    assert rules.table_rules == {}
    assert rules.column_rules == {}


def test_nonexistent_rules_dir_raises_rule_error(tmp_path: Path):
    missing = tmp_path / "does_not_exist"
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=missing)
    assert str(missing) in str(exc_info.value)


def test_rules_dir_that_is_a_file_raises_rule_error(tmp_path: Path):
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=not_a_dir)
    assert str(not_a_dir) in str(exc_info.value)


# ---------------------------------------------------------------------------
# 失敗路徑
# ---------------------------------------------------------------------------


def test_target_not_two_part_raises_rule_error_naming_key(tmp_path: Path):
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  my_tbl.my_col:
    target: notadotformat
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "my_tbl.my_col" in msg


def test_target_unknown_model_raises_rule_error(tmp_path: Path):
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  my_tbl.my_col:
    target: no.such.model
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "my_tbl.my_col" in msg
    assert "no.such" in msg


def test_target_unknown_field_raises_rule_error(tmp_path: Path):
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  my_tbl.my_col:
    target: res.partner.no_such_field
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "my_tbl.my_col" in msg
    assert "no_such_field" in msg


def test_conflicting_target_across_two_files_raises_rule_error_listing_both_origins(
    tmp_path: Path,
):
    _write(
        tmp_path,
        "a_first.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
""",
    )
    _write(
        tmp_path,
        "b_second.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.name
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "a_first.yaml#cust_mast.cust_no" in msg
    assert "b_second.yaml#cust_mast.cust_no" in msg


def test_same_target_across_two_files_is_not_a_conflict(tmp_path: Path):
    _write(
        tmp_path,
        "a_first.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
""",
    )
    _write(
        tmp_path,
        "b_second.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
""",
    )
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    rule = rules.column_rules[("cust_mast", "cust_no")]
    assert rule.target_model == "res.partner"
    assert rule.target_field == "ref"
    # 決定性：兩個檔案排序後，先讀到的檔案的 origin 勝出。
    assert rule.origin == "a_first.yaml#cust_mast.cust_no"


def test_conflicting_table_target_across_two_files_raises_rule_error_listing_both_origins(
    tmp_path: Path,
):
    _write(
        tmp_path,
        "a_first.yaml",
        """
version: 1
tables:
  cust_mast:
    target_model: res.partner
""",
    )
    _write(
        tmp_path,
        "b_second.yaml",
        """
version: 1
tables:
  cust_mast:
    target_model: product.template
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "a_first.yaml#cust_mast" in msg
    assert "b_second.yaml#cust_mast" in msg


def test_same_table_target_across_two_files_is_not_a_conflict(tmp_path: Path):
    _write(
        tmp_path,
        "a_first.yaml",
        """
version: 1
tables:
  cust_mast:
    target_model: res.partner
""",
    )
    _write(
        tmp_path,
        "b_second.yaml",
        """
version: 1
tables:
  cust_mast:
    target_model: res.partner
""",
    )
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    rule = rules.table_rules["cust_mast"]
    assert rule.target_model == "res.partner"
    # 決定性：兩個檔案排序後，先讀到的檔案的 origin 勝出。
    assert rule.origin == "a_first.yaml#cust_mast"


def test_unknown_transform_kind_raises_rule_error(tmp_path: Path):
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
    transform:
      kind: uppercase
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "cust_mast.cust_no" in msg
    assert "uppercase" in msg


def test_value_map_missing_map_raises_rule_error(tmp_path: Path):
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
    transform:
      kind: value_map
      default: true
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "cust_mast.cust_no" in msg
    # 訊息必須點名問題本身（缺的是 `map`），不能只點名出錯的欄位鍵。
    assert "`map`" in msg


def test_constant_missing_value_raises_rule_error(tmp_path: Path):
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
    transform:
      kind: constant
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "cust_mast.cust_no" in msg
    # 訊息必須點名問題本身（缺的是 `value`），不能只點名出錯的欄位鍵。
    assert "`value`" in msg


def test_version_not_one_raises_rule_error(tmp_path: Path):
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 2
columns:
  cust_mast.cust_no:
    target: res.partner.ref
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "bad.yaml" in msg
    # 訊息必須點名實際讀到的壞版本號，以及目前支援的版本號，不能只點名檔名。
    assert "2" in msg
    assert "只支援 1" in msg


def test_deterministic_load_order_conflict_message_is_stable(tmp_path: Path):
    # 兩個檔案的名稱刻意讓字母序與建立順序相反，確認錯誤訊息永遠點名
    # 排序後「先讀到」的檔案，而不是依 OS 目錄列舉順序。
    _write(
        tmp_path,
        "zzz.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
""",
    )
    _write(
        tmp_path,
        "aaa.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.name
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    # aaa.yaml 依檔名排序在前，應先被載入；衝突訊息裡先出現的 origin
    # 必須是 aaa.yaml，不能隨機。
    assert msg.index("aaa.yaml") < msg.index("zzz.yaml")


def test_constant_transform_is_parsed(tmp_path: Path):
    _write(
        tmp_path,
        "ok.yaml",
        """
version: 1
columns:
  cust_mast.cust_kind:
    target: res.partner.is_company
    transform:
      kind: constant
      value: true
""",
    )
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    rule = rules.column_rules[("cust_mast", "cust_kind")]
    assert rule.transform.kind == TransformKind.CONSTANT
    assert rule.transform.value is True


def test_constant_transform_with_false_value_is_parsed(tmp_path: Path):
    # 迴歸測試：`"value" not in body` 必須用 in 判斷、不能改成真值判斷，
    # 否則合法的 `value: false` 會被誤判成「沒填 value」而丟出 RuleError。
    _write(
        tmp_path,
        "ok.yaml",
        """
version: 1
columns:
  cust_mast.cust_kind:
    target: res.partner.is_company
    transform:
      kind: constant
      value: false
""",
    )
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    rule = rules.column_rules[("cust_mast", "cust_kind")]
    assert rule.transform.kind == TransformKind.CONSTANT
    assert rule.transform.value is False


def test_constant_transform_with_null_value_is_parsed(tmp_path: Path):
    # 同上，但驗證 `value: null`——程式碼的註解明白說「哪怕值是
    # None／false／0」都要合法，所以 None 也必須通過而不是被當成缺漏。
    _write(
        tmp_path,
        "ok.yaml",
        """
version: 1
columns:
  cust_mast.cust_kind:
    target: res.partner.ref
    transform:
      kind: constant
      value: null
""",
    )
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    rule = rules.column_rules[("cust_mast", "cust_kind")]
    assert rule.transform.kind == TransformKind.CONSTANT
    assert rule.transform.value is None


# ---------------------------------------------------------------------------
# 最終整分支審查修正 Important 1(a)：`.yml` 副檔名與「規則目錄裡有檔案沒被讀」
# ---------------------------------------------------------------------------


def test_yml_extension_is_read_not_silently_ignored(tmp_path: Path):
    """一位照著 `rules/README.md` 說明去做的貢獻者，若把規則檔存成 `.yml`
    （而不是 `.yaml`），這條規則必須被讀到，而不是悄悄地整份消失。"""
    _write(
        tmp_path,
        "custom.yml",
        """
version: 1
columns:
  my_tbl.my_col:
    target: res.partner.ref
""",
    )
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    assert ("my_tbl", "my_col") in rules.column_rules
    assert rules.column_rules[("my_tbl", "my_col")].origin == "custom.yml#my_tbl.my_col"


def test_yaml_and_yml_cross_file_conflict_is_still_deterministic(tmp_path: Path):
    """`.yaml` 與 `.yml` 混合存在時，跨檔衝突偵測與「先讀到的檔案排序」這兩件
    事都必須維持，不因為多了一種副檔名就失效。"""
    _write(
        tmp_path,
        "a_first.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
""",
    )
    _write(
        tmp_path,
        "b_second.yml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.name
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "a_first.yaml#cust_mast.cust_no" in msg
    assert "b_second.yml#cust_mast.cust_no" in msg


def test_ignored_files_lists_non_rule_files_excluding_readme(tmp_path: Path):
    """規則目錄裡除了 `*.yaml`／`*.yml`／`README.md` 之外的檔案，不會被當成
    規則讀取，但必須被記在 `ignored_files`，讓 CLI 能回報「有個檔案我沒
    讀」，而不是讓它徹底隱形。"""
    _write(tmp_path, "res_partner.yaml", "version: 1\n")
    _write(tmp_path, "README.md", "# 這是文件，不是規則\n")
    _write(tmp_path, "notes.txt", "一些筆記\n")
    _write(tmp_path, "typo.yml.bak", "備份檔\n")

    rules = load_rules(CATALOG, rules_dir=tmp_path)
    assert rules.ignored_files == ["notes.txt", "typo.yml.bak"]


def test_empty_rules_dir_has_no_ignored_files(tmp_path: Path):
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    assert rules.ignored_files == []


def test_only_yaml_and_readme_has_no_ignored_files(tmp_path: Path):
    _write(tmp_path, "res_partner.yaml", "version: 1\n")
    _write(tmp_path, "README.md", "# 文件\n")
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    assert rules.ignored_files == []


# ---------------------------------------------------------------------------
# 最終整分支審查修正 Important 1(c)：transform 的值必須能寫進 mapping.json
# ---------------------------------------------------------------------------


def test_constant_value_that_yaml_resolves_to_a_date_raises_rule_error(tmp_path: Path):
    """`value: 2024-01-01`（沒加引號）會被 PyYAML 解析成 `datetime.date`，
    而不是字串——這種值無法被 `json.dumps` 序列化，必須在載入規則檔的當下
    就被攔下來、點名是哪個檔案的哪個鍵，而不是等到 writer.py 呼叫
    `json.dumps` 時才炸出一段 `TypeError` 的 traceback。"""
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
    transform:
      kind: constant
      value: 2024-01-01
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "bad.yaml" in msg
    assert "cust_mast.cust_no" in msg
    assert "`value`" in msg


def test_constant_value_with_quoted_date_string_is_accepted(tmp_path: Path):
    """同一個日期，只要加引號變成字串，就完全合法——這裡確認驗證沒有誤傷
    正常的字串值。"""
    _write(
        tmp_path,
        "ok.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
    transform:
      kind: constant
      value: "2024-01-01"
""",
    )
    rules = load_rules(CATALOG, rules_dir=tmp_path)
    rule = rules.column_rules[("cust_mast", "cust_no")]
    assert rule.transform.value == "2024-01-01"


def test_value_map_map_key_that_yaml_resolves_to_a_date_raises_rule_error(tmp_path: Path):
    """`map` 的 key 若沒加引號也可能被 YAML 解析成非字串物件（例如日期），
    同樣無法被 `json.dumps` 序列化，必須在載入時被攔下。"""
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  cust_mast.cust_stus:
    target: res.partner.active
    transform:
      kind: value_map
      map:
        2024-01-01: true
        "N": false
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "bad.yaml" in msg
    assert "cust_mast.cust_stus" in msg
    assert "`map`" in msg


def test_value_map_default_that_yaml_resolves_to_a_date_raises_rule_error(tmp_path: Path):
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  cust_mast.cust_stus:
    target: res.partner.active
    transform:
      kind: value_map
      map:
        "Y": true
        "N": false
      default: 2024-01-01
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "bad.yaml" in msg
    assert "cust_mast.cust_stus" in msg
    assert "`default`" in msg


# ---------------------------------------------------------------------------
# 最終整分支審查修正 Important 3：NaN／循環參照兩個 `TypeError` 攔不住的洞
# ---------------------------------------------------------------------------


def test_constant_value_nan_raises_rule_error_instead_of_reaching_writer(tmp_path: Path):
    """`value: .nan` 是合法 YAML（PyYAML 解析成 `float("nan")`），先前這裡的
    `json.dumps(value)` 用預設的 `allow_nan=True` 呼叫，會直接放行——一路
    帶到 `writer.py` 才會把不合法的 JSON 字面值 `NaN`（RFC 8259 不允許）
    靜靜寫進公開的 `mapping.json`，`migrai map` 甚至會回報 exit 0。這裡
    確認 `.nan` 現在在載入規則檔的當下就被攔下來，訊息點名檔案與鍵。
    """
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
    transform:
      kind: constant
      value: .nan
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "bad.yaml" in msg
    assert "cust_mast.cust_no" in msg
    assert "`value`" in msg


def test_constant_value_circular_yaml_anchor_raises_rule_error_not_traceback(tmp_path: Path):
    """`value: &a [*a]` 是合法 YAML 語法（anchor 立刻在自己的序列裡被
    引用），PyYAML 會真的建構出一個自我參照的 Python `list`。
    `json.dumps` 對這種循環參照拋的是 `ValueError`，不是 `TypeError`——先前
    這裡只接住 `TypeError`，這個例外會原樣逃出 `load_rules`，最終讓使用者
    在 `migrai map` 看到一長串 traceback。這裡確認現在會被攔成清楚的
    `RuleError`，而不是逃逸的 `ValueError`。
    """
    _write(
        tmp_path,
        "bad.yaml",
        """
version: 1
columns:
  cust_mast.cust_no:
    target: res.partner.ref
    transform:
      kind: constant
      value: &a [*a]
""",
    )
    with pytest.raises(RuleError) as exc_info:
        load_rules(CATALOG, rules_dir=tmp_path)
    msg = str(exc_info.value)
    assert "bad.yaml" in msg
    assert "cust_mast.cust_no" in msg
    assert "`value`" in msg


# ---------------------------------------------------------------------------
# reference transform（schema 1.1）
# ---------------------------------------------------------------------------


def test_reference_transform_is_parsed(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "r.yaml").write_text(
        "version: 1\n"
        "tables:\n"
        "  ord_mast:\n"
        "    target_model: sale.order\n"
        "columns:\n"
        "  ord_mast.ord_cust_no:\n"
        "    target: sale.order.partner_id\n"
        "    transform:\n"
        "      kind: reference\n"
        "      ref_table: cust_mast\n"
        "      ref_column: cust_no\n",
        encoding="utf-8",
    )
    rule_set = load_rules(CATALOG, rules_dir)
    rule = rule_set.column_rules[("ord_mast", "ord_cust_no")]
    assert rule.transform.kind is TransformKind.REFERENCE
    assert rule.transform.ref_table == "cust_mast"
    assert rule.transform.ref_column == "cust_no"


def test_reference_transform_without_ref_table_is_rejected(tmp_path: Path):
    """缺 ref_table 必須在載入規則檔的當下就大聲失敗，而不是產出一個
    load 階段才會炸掉、且錯誤訊息指不到規則檔的 mapping。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "r.yaml").write_text(
        "version: 1\n"
        "tables:\n"
        "  ord_mast:\n"
        "    target_model: sale.order\n"
        "columns:\n"
        "  ord_mast.ord_cust_no:\n"
        "    target: sale.order.partner_id\n"
        "    transform:\n"
        "      kind: reference\n"
        "      ref_column: cust_no\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleError) as exc:
        load_rules(CATALOG, rules_dir)
    # 訊息必須點名是哪個檔案的哪個鍵
    assert "r.yaml" in str(exc.value)
    assert "ord_mast.ord_cust_no" in str(exc.value)
    assert "ref_table" in str(exc.value)


def test_reference_transform_without_ref_column_is_rejected(tmp_path: Path):
    """對稱於缺 `ref_table` 的情形：缺 `ref_column` 同樣要點名檔案與鍵。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "r.yaml").write_text(
        "version: 1\n"
        "tables:\n"
        "  ord_mast:\n"
        "    target_model: sale.order\n"
        "columns:\n"
        "  ord_mast.ord_cust_no:\n"
        "    target: sale.order.partner_id\n"
        "    transform:\n"
        "      kind: reference\n"
        "      ref_table: cust_mast\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleError) as exc:
        load_rules(CATALOG, rules_dir)
    assert "r.yaml" in str(exc.value)
    assert "ord_mast.ord_cust_no" in str(exc.value)
    assert "ref_column" in str(exc.value)


def test_reference_transform_with_empty_ref_table_is_rejected(tmp_path: Path):
    """空字串跟完全沒填一樣不合法——`ref_table: ""` 不應該悄悄通過。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "r.yaml").write_text(
        "version: 1\n"
        "columns:\n"
        "  ord_mast.ord_cust_no:\n"
        "    target: res.partner.ref\n"
        "    transform:\n"
        "      kind: reference\n"
        "      ref_table: ''\n"
        "      ref_column: cust_no\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleError) as exc:
        load_rules(CATALOG, rules_dir)
    assert "ref_table" in str(exc.value)


# ---------------------------------------------------------------------------
# 最終整分支審查修正 Minor 5：點開頭的檔案（如 macOS 的 .DS_Store）不算「可能
# 打錯副檔名」的候選
# ---------------------------------------------------------------------------


def test_dotfile_in_rules_dir_is_not_listed_as_ignored(tmp_path: Path):
    """這個專案在 macOS 上開發，`rules/` 目錄下出現 `.DS_Store` 是作業系統的
    正常產物，不是貢獻者把副檔名打錯字——不該被回報成「這通常代表副檔名打
    錯字」而讓使用者去檢查一個根本不存在的問題。"""
    _write(tmp_path, "res_partner.yaml", "version: 1\n")
    _write(tmp_path, ".DS_Store", "二進位垃圾\n")
    _write(tmp_path, ".gitkeep", "")

    rules = load_rules(CATALOG, rules_dir=tmp_path)
    assert rules.ignored_files == []
