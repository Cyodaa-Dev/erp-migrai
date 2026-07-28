"""欄位名正規化與名稱相似度評分——`map` 階段兩個啟發式訊號之一。

Task 6 的引擎對一個沒有命中 Task 3 規則檔的來源欄位，會用

    confidence = round(100 * name_score * type_score)

把這個模組的分數與型別相容分數**相乘**，不是加權平均——任一邊是 0，結果
就是 0。所以這個模組回傳 `0.0` 不代表「算壞了」，而是**拒絕用猜的**：那
一欄會被引擎丟到 `unmapped` 或 `needs_human`，由人類寫一條 Task 3 的規則
來對應。

**這裡刻意不做語意猜測。** 契約測試表裡有五筆分數是 `0.0`，但在真實世界
都是正確的對應（`cust_no`→`ref`、`cust_addr`→`street`、`cust_stus`→
`active`）：字面上真的看不出關聯，硬塞一份語意字典去「猜對」這幾筆，只會
讓分數變得不可解釋，還會在別人的 schema 上亂猜出錯誤的高分。這正是
`rules/` 規則檔存在的理由——語意由人決定，這裡只比對字面。

貢獻者最容易需要編輯的就是這個模組（另一個是 Task 5 的型別相容表），因為
每個 T100 佈署站的命名慣例都不一樣。要新增自己環境裡看得懂、但這份表還
不認得的縮寫，直接編輯下面的 `ABBREVIATIONS`；不需要碰任何評分邏輯。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 縮寫展開對照表
# ---------------------------------------------------------------------------
#
# 來源分兩類：
#   1. T100（鼎新 Genero 4GL + Informix）常見的欄位命名縮寫——`no`／`nm`／
#      `amt`／`qty`／`dt`／`addr`／`tel`／`stus`／`ubn`／`cust`／`prod`／
#      `ord`／`seq`／`desc`／`ref`，多半是 Informix 時代為了省字元長度的
#      命名習慣，`ubn`（統一編號）則是台灣特有的營業稅籍編號代稱。
#   2. Odoo 17 目標詞彙——展開後的詞（`name`／`date`／`phone`／`status`／
#      `vat` 等）本身也收進表裡，映到自己：這樣「已經是完整詞」與「還沒
#      展開的縮寫」共用同一份查表邏輯，不必另外判斷。
#
# 這是刻意收斂到最小集合的表：只收 task-4-brief.md 列出的最低需求，不多猜。
# 每個新的縮寫都是「賭自己這條在別人的 schema 上也成立」，所以只在真的
# 遇到、且明確無歧義時才加。
ABBREVIATIONS: dict[str, str] = {
    "no": "number",
    "nm": "name",
    "name": "name",
    "amt": "amount",
    "qty": "quantity",
    "dt": "date",
    "date": "date",
    "addr": "address",
    "tel": "phone",
    "phone": "phone",
    "stus": "status",
    "status": "status",
    "ubn": "vat",  # 統一編號＝台灣的營業稅籍編號，對應 Odoo 的 res.partner.vat
    "cust": "customer",
    "prod": "product",
    "ord": "order",
    "seq": "sequence",
    "desc": "description",
    "ref": "reference",
}

# 偵測 camelCase 的大小寫交界：小寫或數字後面緊接大寫，例如 custName 的
# t→N。用零寬斷言在交界處插入底線，讓底線切詞與大小寫切詞可以共用同一次
# `split("_")`，不必另外維護一套切詞邏輯。
_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# 純數字尾綴，例如 `pmaa001` 的 `001`、`no1` 的 `1`。T100 的代碼式命名常見
# 這種尾綴，且尾綴本身不帶語意（流水號、版本號），故剝除。
_TRAILING_DIGITS_RE = re.compile(r"[0-9]+$")


def normalize(name: str) -> list[str]:
    """把欄位名切成正規化的 token 清單。

    步驟（順序見 task-4-brief.md 的 ruling 3）：

    1. 標出大小寫交界（在轉小寫「之前」做，否則 `custName` 轉成
       `custname` 後就再也分不出 `cust`／`Name` 的交界，`custName` 與
       `cust_name` 就不可能切出同一份 token 清單——這與契約表要求
       `custName` vs `cust_name` → `1.0` 矛盾。所以「大小寫交界的偵測」
       必須先於「轉小寫」，才能同時滿足規格「一律小寫」與「照大小寫交界
       切詞」兩條——偵測時看的是原始大小寫，轉小寫只發生在切完之後、
       產出的 token 上。）
    2. 轉小寫，並以底線（含上一步插入的交界底線）切詞。
    3. 剝除每個 token 的純數字尾綴。
    4. 丟掉剝除後變成空字串的 token（純數字、純底線都會走到這裡變空）。
    5. 展開常見縮寫（見 `ABBREVIATIONS`）。

    回傳的清單保留來源出現順序，不依賴 set 的迭代順序——同樣輸入永遠得到
    逐位元組相同的輸出。是否相似的比對（`name_score`）才把這份清單當成
    集合來看。
    """
    marked = _CASE_BOUNDARY_RE.sub("_", name)
    lowered = marked.lower()

    tokens: list[str] = []
    for raw_token in lowered.split("_"):
        stripped = _TRAILING_DIGITS_RE.sub("", raw_token)
        if not stripped:
            continue
        tokens.append(ABBREVIATIONS.get(stripped, stripped))
    return tokens


def _ordered_unique(tokens: list[str]) -> list[str]:
    """依出現順序去重，不透過 set 迭代——確保任何拿來組理由字串的清單都是
    決定性的（set 迭代順序在不同 process 之間不保證一致）。
    """
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result


def name_score(source_column: str, target_field: str) -> tuple[float, str]:
    """比較兩個欄位名，回傳 `(0.0 到 1.0 的分數, 繁中理由字串)`。

    對任何輸入都不拋例外——`map` 引擎的契約是「絕不拋出」，這個函式是引擎
    對每一欄都會呼叫的訊號之一，任何一次意外的例外都會讓整個 map 階段中斷。
    `normalize` 本身只做字串運算，不會對任何 `str` 輸入拋出例外，所以這裡
    不需要、也不應該用 `except Exception` 去吞掉例外（那會掩蓋真正的邏輯
    錯誤）。

    評分規則（見 task-4-brief.md 的行為規格表）：

    - 任一邊正規化後沒有 token → `0.0`（空集合談不上「相似」，硬湊分數是
      在說謊）。
    - 兩邊 token 集合完全相同（不比較順序）→ `1.0`。
    - 其中一方是另一方的子集，且交集非空 → `0.8`。
    - 有交集但互不為子集 → `0.6 * (交集大小 / 聯集大小)`。
    - 交集為空 → `0.0`——這正是規則檔存在的理由：字面上看不出關聯的欄位，
      只能由人類寫規則，不能靠猜。
    """
    source_tokens = normalize(source_column)
    target_tokens = normalize(target_field)

    source_set = set(source_tokens)
    target_set = set(target_tokens)

    if not source_set or not target_set:
        return (
            0.0,
            f"『{source_column}』正規化後為 {source_tokens}，"
            f"『{target_field}』正規化後為 {target_tokens}："
            "至少一方沒有可比對的 token（原始輸入可能是空字串、純底線或純數字），"
            "無法判斷相似度，判 0.0",
        )

    if source_set == target_set:
        return (
            1.0,
            f"正規化後 token 集合相同（不比較順序）：來源 {source_tokens}，目標 {target_tokens}",
        )

    intersection = source_set & target_set

    if not intersection:
        return (
            0.0,
            f"正規化後無交集：來源 {source_tokens}，目標 {target_tokens}——"
            "字面上看不出關聯，只能靠規則檔手動對應，不能用猜的",
        )

    # 子集分支與部分重疊分支都要顯示「交集」，兩邊算的是同一件事——只算一次，
    # 分支再各自決定怎麼用（Minor 4：原本兩邊各自重算一次同樣的 comprehension）。
    intersection_ordered = _ordered_unique([t for t in source_tokens if t in target_set])

    if source_set <= target_set or target_set <= source_set:
        return (
            0.8,
            f"正規化後其中一方 token 是另一方的子集，交集 {intersection_ordered}："
            f"來源 {source_tokens}，目標 {target_tokens}",
        )

    union_size = len(source_set | target_set)
    score = 0.6 * (len(intersection) / union_size)
    union_ordered = _ordered_unique(
        source_tokens + [t for t in target_tokens if t not in source_set]
    )
    return (
        score,
        f"正規化後有交集但互不包含，交集 {intersection_ordered}"
        f"（{len(intersection)}）／聯集 {union_ordered}（{union_size}），"
        f"分數 = 0.6 × {len(intersection)}/{union_size}："
        f"來源 {source_tokens}，目標 {target_tokens}",
    )
