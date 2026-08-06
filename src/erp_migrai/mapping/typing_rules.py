"""來源型別與 Odoo 型別的相容度評分——`map` 階段兩個啟發式訊號之一。

Task 6 的引擎對一個沒有命中 Task 3 規則檔的來源欄位，會用

    confidence = round(100 * name_score * type_score)

把這個模組的分數與 `naming.name_score` **相乘**，不是加權平均——任一邊是
0，結果就是 0。所以這個模組回傳 `0.0` 不代表「算壞了」，而是**拒絕用猜
的**：一個欄位名對得完美、但型別根本裝不下來源值的對應，不該被自動採用；
那一欄會被引擎丟到 `unmapped` 或 `needs_human`，由人類決定要不要寫轉換
規則。這正是這個函式存在的理由。

`type_score` 只回傳三種值：

- `1.0` 相容——來源值可無損寫入目標欄位。
- `0.5` 有損但可行——需要人確認（例如小數寫進整數欄位、字元代碼寫進
  selection）。
- `0.0` 不相容——不該自動採用。這裡再細分四種成因，理由字串會分別點名：
  「不相容」（型別語意衝突，例如日期沒有數值語意）、
  「無法辨識的來源型別」（`raw` 不在下面 `_SOURCE_TYPE_FAMILIES` 認得的
  清單內）、
  「不是合法的 Odoo 欄位型別」（`odoo_type` 不在 catalog 的
  `ALLOWED_FIELD_TYPES` 內）、
  以及三種關聯型別（`many2one`／`one2many`／`many2many`）共用的
  「需要先解析成 Odoo id，map 階段做不到」。

**刻意的設計決定：長度與 scale 都不影響分數。**

- 長度不影響分數：`CHAR(200)` 對 `char` 跟 `CHAR(10)` 對 `char` 一樣是
  `1.0`。Odoo 的 `char` 沒有預設長度上限，而來源端的長度限制（Informix
  的 `VARCHAR(n)`）是來源端自己的事，不是型別相容性問題。如果未來要檢查
  「來源長度是否超過某個實際限制」，那是 verify 階段的工作，不屬於這裡。
- scale 不影響分數：`DECIMAL(15,0)` 對 `integer` 仍是 `0.5`，即使
  `scale=0` 在數學上意味著沒有小數部分。原因是 `parse_source_type` 回傳
  的參數只給後續階段參考，這個函式的評分只看型別名稱本身，不看參數——
  一致地保守勝過看似聰明但無法驗證：`scale=0` 究竟是「這欄真的沒有小數」
  還是「剛好這批資料沒出現小數」，在 map 階段看不出來，硬區分只會製造出
  看起來很聰明、實際上是猜的例外規則。這條決定與長度的決定同一份精神，
  一併寫在這裡，避免下一個人以為是漏了。

這裡跟 `naming.py` 一樣**刻意不做語意猜測**：相容矩陣只覆蓋型別本身的
資料容納能力，不管欄位名稱暗示了什麼語意（例如一個 `CHAR(1)` 欄位名叫
`is_active` 也一樣只能拿到「需要值轉換」的 `0.5`，不會因為名字像布林就
拿到 `1.0`）——語意訊號是 `naming.name_score` 的事，這裡只管型別。

貢獻者最容易需要編輯的就是這個模組（另一個是 Task 4 的 `naming.py`），
因為每個 T100 佈署站可能出現這份表還不認得的 Informix 型別名稱。要新增
自己環境裡看得到、但這份表還不認得的型別，直接編輯下面的
`_SOURCE_TYPE_FAMILIES`（必要時新增家族並在 `_COMPATIBILITY` 補上該
家族對 13 個 Odoo 型別的完整評分）；不需要碰 `type_score` 的邏輯本身。
"""

from __future__ import annotations

import re

from erp_migrai.mapping.catalog import ALLOWED_FIELD_TYPES

# ---------------------------------------------------------------------------
# parse_source_type：把 "DECIMAL(15,2)" 這種原始型別字串拆成
# (型別名稱大寫, 參數 tuple)。
# ---------------------------------------------------------------------------
#
# 只接受「識別字（可選）(數字(, 數字)?)」這個形狀，且整段輸入都要落在這個
# 形狀內（頭尾都錨定），否則一律視為無法解析。這是刻意的：不去猜測
# "CHAR(abc)"、未終止的 "CHAR(" 或帶第三個參數的 "CHAR(10,20,30)" 該怎麼
# 修正——那些都是 schema.sql 打字錯誤或這裡沒預期到的語法，猜一個「看起來
# 合理」的修正只會製造出不存在的型別資訊。
_TYPE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:\(\s*(?P<len>[0-9]+)\s*(?:,\s*(?P<scale>[0-9]+)\s*)?\))?\s*$"
)


def parse_source_type(raw: str) -> tuple[str, tuple[int, ...]]:
    """把來源型別字串拆成 `(型別名稱大寫, 參數 tuple)`。

    大小寫不敏感（回傳一律大寫），容忍型別名稱與括號、逗號之間的空白。

    對任何無法解析的輸入（空字串、純空白、括號不成對、參數不是純數字、
    超過兩個參數等）一律回傳 `(原字串大寫, ())`，不拋例外——這是
    `type_score` 判斷「無法辨識的來源型別」的依據：一個無法解析的型別
    字串，在大寫化之後幾乎不可能剛好撞上 `_SOURCE_TYPE_FAMILIES` 裡的
    某個已知型別名稱，所以會自然地落入「無法辨識」分支，而不需要另外
    設一個「是否解析成功」的旗標。

    回傳的參數只給後續階段（例如 verify 階段要檢查長度）參考；
    `type_score` 本身刻意不使用這些參數（見模組 docstring）。
    """
    match = _TYPE_RE.match(raw)
    if match is None:
        return raw.upper(), ()

    name = match.group("name").upper()
    params: list[int] = []

    length = match.group("len")
    if length is not None:
        params.append(int(length))
        scale = match.group("scale")
        if scale is not None:
            params.append(int(scale))

    return name, tuple(params)


# ---------------------------------------------------------------------------
# 來源型別家族。
# ---------------------------------------------------------------------------
#
# 刻意收斂到最小集合：只收 task-5-brief.md 明確列出的 Informix 型別名稱
# ——CHAR／VARCHAR／DECIMAL／INTEGER 來自 brief 點名的「語料庫真實範例」
# （corpus/case01_partner/src/schema.sql 目前只實際出現 CHAR／VARCHAR／
# DECIMAL；INTEGER 是 brief 舉例提到但這份語料尚未出現的型別，先一併收進
# 表裡，等真的遇到再驗證），SMALLINT／DATE／DATETIME 則是契約測試表額外
# 要求支援的型別。Informix 還有 BIGINT／NUMERIC／FLOAT／MONEY／SERIAL／
# BOOLEAN／TEXT／BYTE 等型別，這裡刻意不收——每多收一個都是在賭「這個
# 型別在別人的 schema 上也適用同一套家族分類」，沒有語料佐證就不多猜。
# 這與 `naming.py` 的 `ABBREVIATIONS` 是同一種態度：語意（這裡是「這個
# 型別該歸進哪個家族」）由人決定，程式只認已經驗證過的清單；任何不在
# 這裡的型別名稱，`type_score` 一律判 `0.0` 並指名「無法辨識的來源型別」，
# 而不是塞進一個可能猜錯的家族。
_CHARACTER = "character"
_DECIMAL = "decimal"
_INTEGER = "integer"
_DATE = "date"
_DATETIME = "datetime"

_SOURCE_TYPE_FAMILIES: dict[str, str] = {
    "CHAR": _CHARACTER,
    "VARCHAR": _CHARACTER,
    "DECIMAL": _DECIMAL,
    "INTEGER": _INTEGER,
    "SMALLINT": _INTEGER,
    "DATE": _DATE,
    "DATETIME": _DATETIME,
}

# ---------------------------------------------------------------------------
# 相容矩陣：家族 × 13 個 Odoo 型別，逐格明列，不靠 fall-through 拿分數。
# ---------------------------------------------------------------------------
#
# 每一格是 `(分數, 理由片語)`。理由片語會在 `type_score` 組裝進完整句子，
# 但片語本身已經帶著「有損」／「不相容」的字眼，讓 mapping.csv 的審核者
# 一眼分得出兩者差異（見 task-5-brief.md ruling 5）。
#
# 三種關聯型別（many2one／one2many／many2many）在**每個**家族都是同一句
# 理由：寫入它們需要先把來源值解析成 Odoo 的內部 id，這是 `load` 階段才
# 做得到的事，`map` 階段沒有資料庫可查，所以一律 0.0。用 `_with_relational`
# 統一附加這三格，而不是在下面五個家族各自手打三次——這樣「三種關聯型別
# 待遇完全相同」是結構上保證的，不會有某個家族漏加、或某個家族手滑打成
# 不同分數的風險。
_RELATIONAL_REASON = "需要先解析成 Odoo id，map 階段做不到，判定不相容"
_RELATIONAL_ODOO_TYPES = ("many2one", "one2many", "many2many")


def _with_relational(
    entries: dict[str, tuple[float, str]],
) -> dict[str, tuple[float, str]]:
    merged = dict(entries)
    for odoo_type in _RELATIONAL_ODOO_TYPES:
        merged[odoo_type] = (0.0, _RELATIONAL_REASON)
    return merged


_COMPATIBILITY: dict[str, dict[str, tuple[float, str]]] = {
    # 字元類（CHAR／VARCHAR）：來源值本身就是字串，寫進同為字串的欄位
    # 無損；寫進數值／日期需要解析，解析可能失敗，判不相容；寫進
    # boolean／selection 是「值剛好對得上」的巧合，需要人確認。
    _CHARACTER: _with_relational(
        {
            "char": (1.0, "字元對字元，直接相容"),
            "text": (
                1.0,
                "char 進 text 無損（text 沒有長度上限，長度不影響分數）",
            ),
            # 這一格是整張表裡最容易被誤判成無損的：html 也是「字串」，看起來
            # 跟 text 一樣。但 Odoo 的 html 欄位會被當標記渲染——換行字元不會
            # 呈現成換行，`<` 與 `&` 會被當成標記的開頭。寫進去不會報錯，
            # 壞的是呈現，屬於靜默損壞，所以不給滿分。
            "html": (
                0.5,
                "純文字進 html 欄位不會報錯但會靜默走樣：換行不呈現、`<` 與 `&` "
                "被當標記解讀；需人確認是否先做 HTML 跳脫與換行轉 `<br>`",
            ),
            "integer": (0.0, "字元轉數字需要解析，格式不保證成立，判定不相容"),
            "float": (0.0, "字元轉數字需要解析，格式不保證成立，判定不相容"),
            "monetary": (0.0, "字元轉數字需要解析，格式不保證成立，判定不相容"),
            "boolean": (
                0.5,
                "字元轉布林（例如 'Y'/'N'）需要值轉換才能寫入，有損但可行，需人確認",
            ),
            "date": (0.0, "字元轉日期需要解析，格式不保證成立，判定不相容"),
            "datetime": (0.0, "字元轉日期時間需要解析，格式不保證成立，判定不相容"),
            "selection": (
                0.5,
                "字元代碼可能對得上 selection 的選項值，有損但可行，需人確認",
            ),
        }
    ),
    # 小數（DECIMAL）：寫進浮點類欄位無損；寫進整數會丟小數部分；寫進
    # 字串是「可行但通常不是意圖」；寫進日期／selection 沒有語意基礎。
    _DECIMAL: _with_relational(
        {
            "char": (0.5, "數字轉字串可行但通常不是意圖，有損，需人確認"),
            "text": (0.5, "數字轉字串可行但通常不是意圖，有損，需人確認"),
            "html": (
                0.5,
                "數字轉字串可行但通常不是意圖，有損，需人確認；目標是 html 欄位，"
                "數字本身不含需跳脫的字元，沒有額外的標記風險",
            ),
            "integer": (0.5, "小數部分會被丟棄，有損但可行，需人確認"),
            "float": (1.0, "同為浮點數值，直接相容"),
            "monetary": (1.0, "monetary 本質是帶幣別的浮點數，直接相容"),
            "boolean": (
                0.5,
                "數字轉布林（例如 0/1）需要值轉換，有損但可行，需人確認",
            ),
            "date": (0.0, "數值沒有日期語意，判定不相容"),
            "datetime": (0.0, "數值沒有日期語意，判定不相容"),
            "selection": (0.0, "小數是連續數值而非離散代碼，判定不相容"),
        }
    ),
    # 整數（INTEGER／SMALLINT）：跟 DECIMAL 同一套邏輯，差別是整數寫進
    # 整數欄位無損（沒有小數可丟），且整數代碼比小數更常被拿來當
    # selection 的離散代碼，所以給 0.5 而不是 DECIMAL 的 0.0。
    _INTEGER: _with_relational(
        {
            "char": (0.5, "數字轉字串可行但通常不是意圖，有損，需人確認"),
            "text": (0.5, "數字轉字串可行但通常不是意圖，有損，需人確認"),
            "html": (
                0.5,
                "數字轉字串可行但通常不是意圖，有損，需人確認；目標是 html 欄位，"
                "數字本身不含需跳脫的字元，沒有額外的標記風險",
            ),
            "integer": (1.0, "同為整數，直接相容"),
            "float": (1.0, "整數是浮點數的特例，直接相容"),
            "monetary": (
                1.0,
                "monetary 本質是帶幣別的浮點數，整數可無損寫入，直接相容",
            ),
            "boolean": (
                0.5,
                "數字轉布林（例如 0/1）需要值轉換，有損但可行，需人確認",
            ),
            "date": (0.0, "數值沒有日期語意，判定不相容"),
            "datetime": (0.0, "數值沒有日期語意，判定不相容"),
            "selection": (
                0.5,
                "整數代碼可能對得上 selection 的選項值，有損但可行，需人確認",
            ),
        }
    ),
    # 日期（DATE）：寫進 date 無損；寫進 datetime 等於時間部分補零，無損；
    # 寫進字串需要決定格式，有損但可行；其餘（數值／布林／selection）
    # 沒有語意基礎。
    _DATE: _with_relational(
        {
            "char": (0.5, "日期轉字串需要決定格式，有損但可行，需人確認"),
            "text": (0.5, "日期轉字串需要決定格式，有損但可行，需人確認"),
            "html": (
                0.5,
                "日期轉字串需要決定格式，有損但可行，需人確認；目標是 html 欄位，"
                "日期字串不含需跳脫的字元，沒有額外的標記風險",
            ),
            "integer": (0.0, "日期沒有數值語意，判定不相容"),
            "float": (0.0, "日期沒有數值語意，判定不相容"),
            "monetary": (0.0, "日期沒有數值語意，判定不相容"),
            "boolean": (0.0, "日期沒有真偽語意，判定不相容"),
            "date": (1.0, "同為日期，直接相容"),
            "datetime": (1.0, "日期進 datetime，時間部分補零，無損"),
            "selection": (0.0, "日期不是離散代碼，判定不相容"),
        }
    ),
    # 日期時間（DATETIME）：寫進 datetime 無損；寫進 date 會丟時間部分；
    # 其餘跟 DATE 家族同一套理由。
    _DATETIME: _with_relational(
        {
            "char": (0.5, "日期時間轉字串需要決定格式，有損但可行，需人確認"),
            "text": (0.5, "日期時間轉字串需要決定格式，有損但可行，需人確認"),
            "html": (
                0.5,
                "日期時間轉字串需要決定格式，有損但可行，需人確認；目標是 html 欄位，"
                "日期時間字串不含需跳脫的字元，沒有額外的標記風險",
            ),
            "integer": (0.0, "日期時間沒有數值語意，判定不相容"),
            "float": (0.0, "日期時間沒有數值語意，判定不相容"),
            "monetary": (0.0, "日期時間沒有數值語意，判定不相容"),
            "boolean": (0.0, "日期時間沒有真偽語意，判定不相容"),
            "date": (0.5, "時間部分會被丟棄，有損但可行，需人確認"),
            "datetime": (1.0, "同為日期時間，直接相容"),
            "selection": (0.0, "日期時間不是離散代碼，判定不相容"),
        }
    ),
}


def type_score(source_type: str, odoo_type: str) -> tuple[float, str]:
    """比較來源型別與 Odoo 目標型別，回傳 `(0.0／0.5／1.0, 繁中理由字串)`。

    對任何輸入都不拋例外——`map` 引擎的契約是「絕不拋出」。`parse_source_type`
    本身不拋例外，這裡的其餘操作只是 frozenset／dict 查詢與字串組裝，同樣
    不會拋出，所以不需要、也不應該用 `except Exception` 去吞掉例外（那會
    掩蓋真正的邏輯錯誤，例如相容矩陣漏掉某個家族的某個 Odoo 型別）。

    評分規則（見 task-5-brief.md 的行為規格與相容矩陣）：

    1. `odoo_type` 不在 `ALLOWED_FIELD_TYPES`（catalog 的合法欄位型別
       封閉集合）內 → `0.0`，理由指出「不是合法的 Odoo 欄位型別」。
    2. `source_type` 解析後的型別名稱不在 `_SOURCE_TYPE_FAMILIES` 認得的
       家族清單內 → `0.0`，理由指出「無法辨識的來源型別」。
    3. 兩邊都合法 → 查 `_COMPATIBILITY[家族][odoo_type]`，這張表對每個
       已辨識家族、每個 `ALLOWED_FIELD_TYPES` 裡的型別都有明確的一格，
       沒有任何一格是靠 fall-through 拿到非零分數。
    """
    if odoo_type not in ALLOWED_FIELD_TYPES:
        allowed = ", ".join(sorted(ALLOWED_FIELD_TYPES))
        return (
            0.0,
            f"目標型別 `{odoo_type}` 不是合法的 Odoo 欄位型別（允許：{allowed}），判定不相容",
        )

    base_name, _params = parse_source_type(source_type)
    family = _SOURCE_TYPE_FAMILIES.get(base_name)
    if family is None:
        return (
            0.0,
            f"來源型別 `{source_type}`（正規化後為 `{base_name}`）是無法辨識的來源型別，"
            "不在已知的來源型別家族清單內，判定不相容",
        )

    score, phrase = _COMPATIBILITY[family][odoo_type]
    return (
        score,
        f"來源型別 `{source_type}`（家族：{family}）→ Odoo 型別 `{odoo_type}`：{phrase}",
    )
