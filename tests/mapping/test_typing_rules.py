"""typing_rules.parse_source_type／typing_rules.type_score 的測試。

契約表（見 `.superpowers/sdd/task-5-brief.md`）裡的每一列都是一個獨立的
參數化案例，期望分數不得為了遷就實作而調整。表格裡「理由要提到」欄只是
提示理由該傳達的意思，不是要求逐字重現的字串——這裡另外用專門的測試，
針對各分支各自挑一段不會跟其他分支撞名的子字串（同 test_naming.py 的作法）。

另外涵蓋：

- `type_score` 對任何輸入都不得拋例外（含空字串、純底線、malformed 括號、非 ASCII）。
- `parse_source_type` 對任何輸入都不得拋例外，且對無法解析的輸入回傳
  `(原字串大寫, ())`。
- 長度不影響分數（`CHAR(200)` 與 `CHAR(10)` 對 `char` 同分）。
- scale 不影響分數（`DECIMAL(15,0)` 對 `integer` 仍是 0.5，不會因為 scale=0
  就升成 1.0）。
- 目標型別不在 catalog 允許集合內時回傳 0.0，理由要指出這件事。
- 相容矩陣涵蓋每個已辨識家族 × 全部 12 個 Odoo 型別，沒有任何一格是靠
  fall-through 拿到非零分數。
"""

from __future__ import annotations

import pytest

from erp_migrai.mapping.catalog import ALLOWED_FIELD_TYPES
from erp_migrai.mapping.typing_rules import parse_source_type, type_score

# ---------------------------------------------------------------------------
# 契約表：見 task-5-brief.md「測試案例」一節，逐列照抄，不得調整期望值。
# ---------------------------------------------------------------------------

CONTRACT_CASES = [
    ("CHAR(10)", "char", 1.0),
    ("VARCHAR(60)", "char", 1.0),
    ("VARCHAR(120)", "text", 1.0),
    ("CHAR(1)", "boolean", 0.5),
    ("DECIMAL(15,2)", "float", 1.0),
    ("DECIMAL(15,2)", "monetary", 1.0),
    ("DECIMAL(15,2)", "integer", 0.5),
    ("INTEGER", "integer", 1.0),
    ("INTEGER", "float", 1.0),
    ("SMALLINT", "integer", 1.0),
    ("DATE", "date", 1.0),
    ("DATE", "datetime", 1.0),
    ("DATETIME", "date", 0.5),
    ("CHAR(10)", "many2one", 0.0),
    ("DECIMAL(15,2)", "char", 0.5),
    ("CHAR(10)", "integer", 0.0),
    ("CHAR(10)", "date", 0.0),
    ("UNKNOWN_TYPE", "char", 0.0),
    ("CHAR(10)", "selection", 0.5),
]


@pytest.mark.parametrize(("source_type", "odoo_type", "expected"), CONTRACT_CASES)
def test_type_score_contract_table(source_type: str, odoo_type: str, expected: float) -> None:
    score, reason = type_score(source_type, odoo_type)
    assert score == pytest.approx(expected)
    assert isinstance(reason, str)
    assert reason  # 理由字串不得是空的——人工審核必須看得到判斷依據。


# ---------------------------------------------------------------------------
# 契約表「理由要提到」欄位的具體子字串斷言。
# ---------------------------------------------------------------------------


def test_reason_names_char_to_text_lossless():
    _, reason = type_score("VARCHAR(120)", "text")
    assert "無損" in reason


def test_reason_names_char_to_boolean_needs_conversion():
    _, reason = type_score("CHAR(1)", "boolean")
    assert "需要值轉換" in reason


def test_reason_names_decimal_to_integer_truncation():
    _, reason = type_score("DECIMAL(15,2)", "integer")
    assert "小數部分會被丟棄" in reason


def test_reason_names_datetime_to_date_truncation():
    _, reason = type_score("DATETIME", "date")
    assert "時間部分會被丟棄" in reason


def test_reason_names_char_to_many2one_unresolvable():
    _, reason = type_score("CHAR(10)", "many2one")
    assert "解析成 Odoo id" in reason
    assert "map 階段做不到" in reason


def test_reason_names_decimal_to_char_not_the_intent():
    _, reason = type_score("DECIMAL(15,2)", "char")
    assert "數字轉字串可行但通常不是意圖" in reason


def test_reason_names_unknown_source_type():
    _, reason = type_score("UNKNOWN_TYPE", "char")
    assert "無法辨識的來源型別" in reason


def test_reason_names_char_to_selection_needs_confirmation():
    _, reason = type_score("CHAR(10)", "selection")
    assert "選項值" in reason
    assert "需人確認" in reason


def test_integer_to_selection_is_pinned_at_half_as_the_least_evidenced_cell():
    """整數 → selection 是整張矩陣中唯一沒有契約列背書、純靠類比
    （T100 的整數狀態碼）推出來的一格——implementer 在 task-5-report.md
    裡自己點名這格是「最沒把握」的。exhaustive 測試只斷言
    `score in (0.0, 0.5, 1.0)`，就算這格被悄悄改成 0.0 或 1.0 也一樣會過；
    這裡專門釘住這一格的確切值，讓任何未來的改動都會被看見。
    """
    score, reason = type_score("INTEGER", "selection")
    assert score == 0.5
    assert "選項值" in reason
    assert "需人確認" in reason


# ---------------------------------------------------------------------------
# ruling 5：0.5（有損）／0.0（不相容）的措辭要能區分開。
# ---------------------------------------------------------------------------


def test_lossy_reason_says_lossy_not_incompatible():
    score, reason = type_score("INTEGER", "boolean")
    assert score == 0.5
    assert "有損" in reason


def test_incompatible_reason_says_incompatible_not_lossy():
    score, reason = type_score("DATE", "integer")
    assert score == 0.0
    assert "不相容" in reason


# ---------------------------------------------------------------------------
# 目標型別不合法：不在 ALLOWED_FIELD_TYPES 內一律 0.0，理由要點名這件事。
# ---------------------------------------------------------------------------


def test_invalid_target_type_scores_zero_with_reason():
    score, reason = type_score("CHAR(10)", "not_a_real_odoo_type")
    assert score == 0.0
    assert "不是合法的 Odoo 欄位型別" in reason


@pytest.mark.parametrize("bogus_odoo_type", ["", "CHAR", "Char", "int", "string"])
def test_invalid_target_type_variants_score_zero(bogus_odoo_type: str) -> None:
    score, reason = type_score("INTEGER", bogus_odoo_type)
    assert score == 0.0
    assert "不是合法的 Odoo 欄位型別" in reason


# ---------------------------------------------------------------------------
# ruling 3：三種關聯型別對任何來源家族都是 0.0，理由相同（不得落入字元／
# 數字相容分支）。
# ---------------------------------------------------------------------------

_RECOGNIZED_SOURCE_TYPES = [
    "CHAR(10)",
    "VARCHAR(20)",
    "DECIMAL(15,2)",
    "INTEGER",
    "DATE",
    "DATETIME",
]
_RELATIONAL_ODOO_TYPES = ["many2one", "one2many", "many2many"]


@pytest.mark.parametrize("source_type", _RECOGNIZED_SOURCE_TYPES)
@pytest.mark.parametrize("odoo_type", _RELATIONAL_ODOO_TYPES)
def test_relational_targets_always_zero(source_type: str, odoo_type: str) -> None:
    score, reason = type_score(source_type, odoo_type)
    assert score == 0.0
    assert "Odoo id" in reason


# ---------------------------------------------------------------------------
# 相容矩陣涵蓋每個已辨識家族 × 全部 12 個 Odoo 型別，且分數只可能是三種值。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_type",
    ["CHAR(10)", "VARCHAR(20)", "DECIMAL(15,2)", "INTEGER", "SMALLINT", "DATE", "DATETIME"],
)
@pytest.mark.parametrize("odoo_type", sorted(ALLOWED_FIELD_TYPES))
def test_matrix_is_exhaustive_and_scores_are_one_of_three_values(
    source_type: str, odoo_type: str
) -> None:
    score, reason = type_score(source_type, odoo_type)
    assert score in (0.0, 0.5, 1.0)
    assert reason


# ---------------------------------------------------------------------------
# 長度不影響分數。
# ---------------------------------------------------------------------------


def test_length_does_not_affect_score():
    short_score, _ = type_score("CHAR(10)", "char")
    long_score, _ = type_score("CHAR(200)", "char")
    assert short_score == long_score == 1.0


def test_length_does_not_affect_score_for_varchar():
    score_20, _ = type_score("VARCHAR(20)", "char")
    score_120, _ = type_score("VARCHAR(120)", "char")
    assert score_20 == score_120 == 1.0


# ---------------------------------------------------------------------------
# ruling 4：scale 不影響分數——scale=0 不會讓 DECIMAL 升成對 integer 無損。
# ---------------------------------------------------------------------------


def test_zero_scale_decimal_does_not_upgrade_to_integer_lossless():
    score, _ = type_score("DECIMAL(15,0)", "integer")
    assert score == 0.5


def test_decimal_scale_does_not_affect_score_against_float():
    score_2, _ = type_score("DECIMAL(15,2)", "float")
    score_0, _ = type_score("DECIMAL(15,0)", "float")
    assert score_2 == score_0 == 1.0


# ---------------------------------------------------------------------------
# type_score 對任何輸入都不得拋例外。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_type,odoo_type",
    [
        ("", ""),
        ("   ", "   "),
        ("CHAR(", "char"),
        ("CHAR(abc)", "char"),
        ("DECIMAL(15,2", "float"),
        ("客戶欄位", "char"),
        ("CHAR(10)", "客戶欄位"),
        ("", "char"),
        ("CHAR(10)", ""),
        ("CHAR(-1)", "char"),
        ("CHAR(10,20,30)", "char"),
    ],
)
def test_type_score_never_raises(source_type: str, odoo_type: str) -> None:
    score, reason = type_score(source_type, odoo_type)
    assert 0.0 <= score <= 1.0
    assert isinstance(reason, str)
    assert reason


# ---------------------------------------------------------------------------
# parse_source_type 的行為規格。
# ---------------------------------------------------------------------------


def test_parse_source_type_with_two_params():
    assert parse_source_type("DECIMAL(15,2)") == ("DECIMAL", (15, 2))


def test_parse_source_type_with_one_param():
    assert parse_source_type("CHAR(10)") == ("CHAR", (10,))


def test_parse_source_type_with_no_params():
    assert parse_source_type("INTEGER") == ("INTEGER", ())


def test_parse_source_type_is_case_insensitive():
    assert parse_source_type("integer") == ("INTEGER", ())
    assert parse_source_type("decimal(15,2)") == ("DECIMAL", (15, 2))
    assert parse_source_type("Char(10)") == ("CHAR", (10,))


def test_parse_source_type_tolerates_whitespace():
    assert parse_source_type("  CHAR ( 10 ) ") == ("CHAR", (10,))
    assert parse_source_type(" INTEGER ") == ("INTEGER", ())


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ("", ())),
        ("   ", ("   ", ())),
        ("CHAR(", ("CHAR(", ())),
        ("CHAR(abc)", ("CHAR(ABC)", ())),
        ("DECIMAL(15,2", ("DECIMAL(15,2", ())),
        ("客戶欄位", ("客戶欄位", ())),
        ("CHAR(10,20,30)", ("CHAR(10,20,30)", ())),
        ("CHAR(-1)", ("CHAR(-1)", ())),
    ],
)
def test_parse_source_type_unparseable_input_returns_uppercased_original(
    raw: str, expected: tuple[str, tuple[int, ...]]
) -> None:
    assert parse_source_type(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "CHAR(",
        "CHAR(abc)",
        "DECIMAL(15,2",
        "客戶欄位",
        "CHAR(10,20,30)",
        "CHAR(-1)",
        "\t\n",
    ],
)
def test_parse_source_type_never_raises(raw: str) -> None:
    name, params = parse_source_type(raw)
    assert isinstance(name, str)
    assert isinstance(params, tuple)
    assert all(isinstance(p, int) for p in params)


# ---------------------------------------------------------------------------
# 決定性：同樣輸入永遠得到逐位元組相同的輸出。
# ---------------------------------------------------------------------------


def test_type_score_is_deterministic_across_repeated_calls():
    first = type_score("CHAR(10)", "char")
    second = type_score("CHAR(10)", "char")
    assert first == second


def test_parse_source_type_is_deterministic_across_repeated_calls():
    assert parse_source_type("DECIMAL(15,2)") == parse_source_type("DECIMAL(15,2)")
