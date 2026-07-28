"""naming.normalize／naming.name_score 的測試。

契約表（見 `.superpowers/sdd/task-4-brief.md`）裡的每一列都是一個獨立的
參數化案例，期望分數不得為了遷就實作而調整。另外涵蓋：

- `name_score` 對任何輸入都不得拋例外（含空字串、純底線、純數字、非 ASCII）。
- 行為規格裡「有交集但互不包含」的 0.6 倍 Jaccard 分支——契約表沒有涵蓋到
  這一分支的具體案例，但規格明白列了這條規則，所以額外補一個案例確認它
  真的照規格算，而不是意外從沒被跑到。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from erp_migrai.mapping.naming import name_score, normalize

# ---------------------------------------------------------------------------
# 契約表：見 task-4-brief.md「測試案例」一節，逐列照抄，不得調整期望值。
# ---------------------------------------------------------------------------

CONTRACT_CASES = [
    ("cust_name", "name", 0.8),
    ("cust_no", "ref", 0.0),
    ("cust_ubn", "vat", 0.8),
    ("cust_tel", "phone", 0.8),
    ("cust_addr", "street", 0.0),
    ("cust_stus", "active", 0.0),
    ("order_date", "date_order", 1.0),
    ("custName", "cust_name", 1.0),
    ("pmaa001", "pmaa", 1.0),
    ("product_qty", "product_uom_qty", 0.8),
    ("amt_total", "amount_total", 1.0),
    ("", "name", 0.0),
]


@pytest.mark.parametrize(("source_column", "target_field", "expected"), CONTRACT_CASES)
def test_name_score_contract_table(source_column: str, target_field: str, expected: float) -> None:
    score, reason = name_score(source_column, target_field)
    assert score == pytest.approx(expected)
    assert isinstance(reason, str)
    assert reason  # 理由字串不得是空的——人工審核必須看得到判斷依據。


# ---------------------------------------------------------------------------
# 行為規格額外分支：有交集但互不包含 → 0.6 * (交集大小 / 聯集大小)
# ---------------------------------------------------------------------------


def test_name_score_partial_overlap_uses_jaccard_times_zero_point_six():
    # "prod_amt" → ["product", "amount"]；"amount_total" → ["amount", "total"]。
    # 交集 {"amount"}（1 個），聯集 {"product", "amount", "total"}（3 個），
    # 兩邊互不為對方子集，落入「有交集但互不包含」分支。
    score, reason = name_score("prod_amt", "amount_total")
    assert score == pytest.approx(0.6 * (1 / 3))
    assert reason


# ---------------------------------------------------------------------------
# ruling 2：任一邊正規化後零 token → 分數必須釘死在 0.0。
#
# 這條 ruling 存在的唯一理由，就是防止 "" vs "" 落進「token 集合相同 →
# 1.0」那個分支——兩個空集合在數學上確實相等，若沒有這條 ruling 擋在前面，
# `name_score("", "")` 會回報「完全匹配、信心 1.0」，那是在說謊。
#
# 修正輪次 1 Important 2：先前唯一碰到 ("", "") 的測試是
# `test_name_score_never_raises`，只斷言 `0.0 <= score <= 1.0`——這個範圍
# 連 1.0 都放得進去，等於沒測到這條 ruling。這裡改成直接釘死期望值。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_column,target_field",
    [
        ("", ""),  # 兩邊都是空字串——ruling 2 要擋的典型案例。
        ("cust_name", ""),  # 目標端是空字串（Minor 3 同一個缺口）。
        ("", "cust_name"),  # 來源端是空字串，方向對調。
        ("___", "___"),  # 純底線正規化後也是零 token，不是只有空字串會踩到。
        ("0012", "0012"),  # 純數字尾綴剝除後一樣是零 token。
        ("___", "0012"),  # 兩種「正規化後歸零」的輸入互相搭配。
    ],
)
def test_name_score_pins_zero_for_empty_token_sets(source_column: str, target_field: str) -> None:
    score, _ = name_score(source_column, target_field)
    assert score == 0.0


# ---------------------------------------------------------------------------
# Requirement 5／修正輪次 1 Important 3：理由字串要能指出是哪一條規則命中，
# 而不是只斷言非空——非空的斷言連理由字串退化成通用佔位字串都測不出來。
# 下面五個案例對應 name_score 的五個分支，各自挑一段不會跟其他分支撞名的
# 子字串。刻意不斷言整句話，否則以後任何一次文字微調都會變成測試失敗。
# ---------------------------------------------------------------------------


def test_name_score_reason_names_empty_branch():
    _, reason = name_score("", "name")
    assert "至少一方沒有可比對的 token" in reason


def test_name_score_reason_names_identical_branch():
    _, reason = name_score("order_date", "date_order")
    assert "token 集合相同" in reason


def test_name_score_reason_names_subset_branch():
    _, reason = name_score("cust_name", "name")
    assert "是另一方的子集" in reason


def test_name_score_reason_names_partial_overlap_branch():
    _, reason = name_score("prod_amt", "amount_total")
    assert "有交集但互不包含" in reason


def test_name_score_reason_names_no_overlap_branch():
    _, reason = name_score("cust_no", "ref")
    assert "正規化後無交集" in reason


# ---------------------------------------------------------------------------
# normalize 的行為規格
# ---------------------------------------------------------------------------


def test_normalize_lowercases():
    assert normalize("CUST_NAME") == normalize("cust_name")


def test_normalize_splits_on_underscore_and_case_boundary():
    assert normalize("custName") == ["customer", "name"]
    assert normalize("cust_name") == ["customer", "name"]


def test_normalize_strips_trailing_pure_digit_suffix():
    assert normalize("pmaa001") == ["pmaa"]


def test_normalize_expands_no1_to_number():
    # ruling 3 的推導範例：先剝除純數字尾綴（"no1" → "no"），再展開縮寫（"no" → "number"）。
    assert normalize("no1") == ["number"]


def test_normalize_pure_digit_token_becomes_empty_list():
    assert normalize("001") == []


def test_normalize_pure_underscore_becomes_empty_list():
    assert normalize("___") == []


def test_normalize_empty_string_becomes_empty_list():
    assert normalize("") == []


def test_normalize_output_is_independent_of_pythonhashseed():
    """真正驗證 `normalize` 的輸出順序不依賴 set 迭代順序。

    修正輪次 1 Minor 5：舊版的
    `test_normalize_is_deterministic_across_repeated_calls` 只在同一個
    process 內重複呼叫 `normalize` 多次比對——但同一個 process 內
    `PYTHONHASHSEED` 是固定的，就算實作偷用了 `list(set(...))` 這種依賴
    set 迭代順序的寫法，同一個 process 內重複呼叫還是會拿到彼此一致（但
    跨 process 不保證一致）的結果，這個測試永遠是綠燈、測不出真正的坑。

    這裡改成用 `sys.executable` 開兩個獨立 subprocess、各自指定不同的
    `PYTHONHASHSEED`，分別呼叫 `normalize` 後比較輸出字面——只有這樣才能
    真的抓到「輸出依賴 set 迭代順序」這個缺陷。只用標準庫（`subprocess`／
    `sys`／`os`），沒有新增任何相依套件。
    """
    script = "from erp_migrai.mapping.naming import normalize; print(normalize('cust_addr_seq'))"
    outputs: list[str] = []
    for seed in ("0", "1"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1]
    assert outputs[0] == "['customer', 'address', 'sequence']"


@pytest.mark.parametrize(
    "abbrev,expanded",
    [
        ("no", "number"),
        ("nm", "name"),
        ("amt", "amount"),
        ("qty", "quantity"),
        ("dt", "date"),
        ("addr", "address"),
        ("tel", "phone"),
        ("stus", "status"),
        ("ubn", "vat"),
        ("cust", "customer"),
        ("prod", "product"),
        ("ord", "order"),
        ("seq", "sequence"),
        ("desc", "description"),
        ("ref", "reference"),
        # 下面四筆是「展開後的詞映到自己」的自我映射條目——brief 的縮寫表
        # 明確要求收（`nm／name→name` 等寫法本身就暗示兩個 key 都要收），
        # 之前漏了逐一參數化驗證（修正輪次 1 Important 1）。行為上跟表裡
        # 沒有這幾筆完全一樣（`dict.get(k, k)` 找不到 key 就回傳原字串），
        # 但既然報告聲稱「19 筆全部逐一參數化」，這裡就要讓聲稱成真。
        ("name", "name"),
        ("date", "date"),
        ("phone", "phone"),
        ("status", "status"),
    ],
)
def test_normalize_expands_minimum_abbreviation_table(abbrev: str, expanded: str) -> None:
    assert normalize(abbrev) == [expanded]


# ---------------------------------------------------------------------------
# name_score 對任何輸入都不得拋例外
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_column,target_field",
    [
        ("", ""),
        ("___", "___"),
        ("12345", "67890"),
        ("客戶名稱", "顧客姓名"),
        ("客戶_no", "cust_客戶"),
        ("", "客戶"),
    ],
)
def test_name_score_never_raises(source_column: str, target_field: str) -> None:
    score, reason = name_score(source_column, target_field)
    assert 0.0 <= score <= 1.0
    assert isinstance(reason, str)
    assert reason
