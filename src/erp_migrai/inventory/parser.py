"""逐句解析程式單元的內容。

邏輯語句的切分規則：**一個語句從「首個 token 是已知語句關鍵字」的那一行開始，
延續到下一個這樣的行為止。** 4GL 沒有語句終止符，這條規則簡單、可預測。
已知限制：同一行寫兩個語句無法切分。

這條基本規則不足以正確處理三種常見的多行結構，`_group_statement_lines`
因此加了三個例外（細節與理由見該函式的說明）：
  1. 一句語句裡有未配對的 `(`（例如 `CALL foo(a,` 換行接 `b)`），在括號配平之前
     視為延續——但**有界**：只要接下來那一行的第一個字是已知語句關鍵字，就
     結束目前這句、讓那一行去開新的一句，並強制回報這句括號始終沒配平
     （Task 5 Fix round 1 / Defect 2：早期版本「不管接下來那一行的第一個字是
     什麼都無條件延續」會在原始碼括號沒配平時，把後面整個區塊安靜吞掉、
     不留一絲痕跡）。
  2. 多行 SQL 的子句關鍵字（`FROM`／`WHERE`／`ORDER` 等）不能讓 SQL 語句從中被切碎；
     `DECLARE ... CURSOR FOR` 換行接 `SELECT` 也是同一句（`FOR` 結尾判斷用字界
     比對，不是子字串 `endswith`——後者會被 `mgr_for` 這類以 for 結尾的識別字
     誤觸發，見 `_CURSOR_FOR_SUFFIX`）。子句關鍵字之外，若下一行的第一個字不在
     任何已知子句集合裡，**不能**再用「不是已知語句關鍵字就當延續」這種猜測
     （Task 5 Fix round 2 / Defect 3：這個猜測套用在 `COMMIT`／`ROLLBACK`／
     `EXECUTE` 這些交易或動態 SQL 語句接在一句已完整的 SQL 後面時會出錯——
     這些字本來就不在任何 SQL 子句集合裡，猜測會把它們當成延續、悄悄併入前一句
     SQL 而消失，且不留下任何 unsupported 紀錄）。改用真正的語法訊號：只有當
     累積到目前為止的文字本身「明顯還沒寫完」（結尾是逗號、左括號、算術／比較
     運算子，或 `AND`／`OR`／`NOT`／`IN`／`LIKE`／`BETWEEN`／`IS`／`AS`／`BY`
     這些字，見 `_statement_looks_incomplete`）才視為延續——這與例外 1 的括號
     配平判斷是同一種思路：用「這句話寫完了嗎」的事實去判斷，而不是猜測。
  3. `DEFINE ... RECORD`（非 `RECORD LIKE`）到對應 `END RECORD` 之前，即使中途
     出現 `END` 這種已知語句關鍵字也不能被切碎——但同樣**有界**：若 `END
     RECORD` 始終沒出現，遇到下一句語句關鍵字時就結束並強制回報 RECORD
     未關閉，理由與例外 1 相同。

除了這三個例外之外，**任何無法辨識的關鍵字一律視為一句新的（無法辨識的）語句
起點，絕不天真地併入前一句**。這是刻意的、且與 TDD 過程中發現的一個真實陷阱
直接相關：曾經考慮過的寫法是「無法辨識的關鍵字一律併入前一句」，這對多行 SQL
子句（FROM／WHERE／INTO 這些字本身根本不在任何關鍵字集合裡）剛好湊巧管用，
但套用在 `DEFINE l_cnt INTEGER` 後面接一行看不懂的 `TRY` 時，會把兩者黏成
`"DEFINE l_cnt INTEGER TRY"` 去餵文法，得到一個指向錯誤行號、講錯原因的失敗
回報（"DEFINE 解析失敗" 而不是「TRY 是無法辨識的語句」），`l_cnt` 這個原本能
正確解析的定義也因此憑空消失。這正是本專案不能接受的「安靜地產出錯誤結果」。
因此，合併與否改成明確地依照上述三個結構性理由（括號配平／SQL 語句／RECORD
欄位）判斷，而不是「看不懂就併」的預設值——寧可對真正落在這三種結構之外的
多行、無關鍵字延續句（例如一句沒有用到括號的多行運算式）過度切碎、多報幾筆
無法辨識，也不要讓一句誠實的回報被併成張冠李戴的錯誤訊息。

遮罩（masking）：本模組**只使用** `Block.masked_lines`（`blocks.py` 已經算好的
遮罩後副本）做關鍵字判斷、括號計數、DEFINE 文法解析與 SQL 擷取——絕不在這裡
重新遮罩一次，否則會把 `blocks.py`／`masking.py` 已經回報過的同一批未終止
註解／字串常值異常重複算一次、在輸出裡出現兩份。`Block.lines`（原始文字）
只用在組裝 `UnsupportedSyntax.snippet`，確保使用者看到的永遠是真正的原始碼，
而不是被換成空白的遮罩版本。

每一句的處理只有五種結果：
  1. DEFINE       → 交給 genero.lark 解析成 Definition
  2. SQL 關鍵字   → 交給 sqlref 擷取種類與表名
  3. 已知但不分析 → KNOWN_IGNORED，靜默略過（控制流程與 UI 語句，也包含
     Task 5 Fix round 2 新增的交易／動態 SQL 語句：COMMIT、ROLLBACK、BEGIN、
     PREPARE、EXECUTE、LOCK、UNLOCK、SET、LOAD、UNLOAD、CREATE、DROP、ALTER、
     RENAME、GRANT、REVOKE、INFO、DATABASE）
  4. 其他         → 記錄成 UnsupportedSyntax（呼叫端會發 E-1002）
  5. 括號未配平（正或負）／RECORD 未關閉／語句結尾語法明顯未完成 →
     記錄成 UnsupportedSyntax，且**優先於**第 1～3 種結果判斷：這句累積到的
     文字本身就不完整，不再嘗試交給 DEFINE 文法或 SQL 擷取，也不因為第一個字
     落在 KNOWN_IGNORED 就靜默略過（Task 5 Fix round 1 / Defect 2：KNOWN_IGNORED
     不該是一個能讓真正落差安靜消失的漏洞；Task 5 Fix round 2 / Defect 3 補上
     「結尾語法明顯未完成」與「括號差為負」兩種同一類的結構性訊號）。

第 3 與第 4 的區別很重要：「我認得但不分析」和「我不認得」是兩件不同的事，
誠實回報必須分清楚，否則不支援清單會被雜訊淹沒。
"""

from __future__ import annotations

import re
from pathlib import Path

from lark import Lark, Transformer
from lark.exceptions import LarkError

from erp_migrai.inventory.blocks import Block
from erp_migrai.inventory.models import (
    Definition,
    ProgramUnit,
    SqlStatement,
    UnsupportedSyntax,
    snippet,
)
from erp_migrai.inventory.sqlref import SQL_KEYWORDS, extract_sql

_GRAMMAR = Lark(
    Path(__file__).with_name("genero.lark").read_text(encoding="utf-8"),
    parser="lalr",
    start="start",
)

# 認得但刻意不分析的語句關鍵字：控制流程與 UI。
# 這些出現在程式裡是正常的，不該進不支援清單。
KNOWN_IGNORED = frozenset(
    {
        "AFTER",
        "ALTER",
        "AT",
        "BEFORE",
        "BEGIN",
        "CALL",
        "CASE",
        "CLEAR",
        "CLOSE",
        "COMMAND",
        "COMMIT",
        "CONSTRUCT",
        "CONTINUE",
        "CREATE",
        "DATABASE",
        "DISPLAY",
        "DROP",
        "ELIF",
        "ELSE",
        "END",
        "ERROR",
        "EVERY",
        "EXECUTE",
        "EXIT",
        "FETCH",
        "FIRST",
        "FLUSH",
        "FOR",
        "FOREACH",
        "FORMAT",
        "FREE",
        "GOTO",
        "GRANT",
        "IF",
        "INFO",
        "INITIALIZE",
        "INPUT",
        "LABEL",
        "LET",
        "LOAD",
        "LOCK",
        "MENU",
        "MESSAGE",
        "NEED",
        "NEXT",
        "ON",
        "OPEN",
        "ORDER",
        "OTHERWISE",
        "OUTPUT",
        "PAGE",
        "PAUSE",
        "PREPARE",
        "PRINT",
        "PROMPT",
        "PUT",
        "RENAME",
        "RETURN",
        "REVOKE",
        "ROLLBACK",
        "RUN",
        "SET",
        "SKIP",
        "SLEEP",
        "START",
        "THEN",
        "UNLOAD",
        "UNLOCK",
        "VALIDATE",
        "WHEN",
        "WHENEVER",
        "WHILE",
    }
)

_STATEMENT_KEYWORDS = KNOWN_IGNORED | SQL_KEYWORDS | {"DEFINE"}

# --- Task 5 Resolution 2：多行 SQL 不能被子句關鍵字切碎 -------------------------
#
# 逐句切分的基本規則是「首字是已知語句關鍵字的那一行開始新語句」，但這條規則
# 天真地套用在多行 SQL 上會出錯，因為部分 SQL 子句關鍵字本身也在 KNOWN_IGNORED
# 或 SQL_KEYWORDS 裡：
#   SELECT ... / INTO ... / FROM ... / WHERE ... / ORDER BY ...
# 若 `ORDER`（在 KNOWN_IGNORED 裡）或 `FROM`／`WHERE`（SQL 子句）被當成新語句的
# 開頭，一句完整的多行 SELECT 就會被切成好幾段，其中大部分段落沒有 FROM，
# `extract_sql` 就抓不到表名。因此：只要「目前正在累積的語句」第一個字是
# SQL_KEYWORDS 之一（代表這是一句 SQL），這些子句關鍵字（或任何本來就不在
# `_STATEMENT_KEYWORDS` 裡的字，例如欄位別名、運算式片段）就不能觸發新語句。
#
# 另一個特例是 `DECLARE <name> CURSOR FOR` 換行後緊接著 `SELECT ...`——這是
# 宣告 cursor 时最常見的寫法。`SELECT` 本身在 SQL_KEYWORDS 裡，正常情況下它自己
# 就會開一句新語句；但如果它接在一句以 `FOR` 結尾的 SQL 語句後面，形狀就是
# `DECLARE ... CURSOR FOR` + `SELECT ...`，這時 SELECT 不該另起一句，否則
# DECLARE_CURSOR 會被回報成 tables == []。這條規則刻意只在「上一句以 FOR
# 結尾」時生效，兩個緊鄰但各自獨立的 SELECT 不會被誤併。
#
# Task 5 Fix round 1 / Defect 1：「以 FOR 結尾」必須是「最後一個字就是 FOR」，
# 用字界（word boundary）比對，不能只用 `str.endswith("FOR")`。後者是子字串
# 比對，`mgr_for`／`reason_for`／`used_for`／`waiting_for` 這類在 ERP schema
# 裡很常見的識別字，其最後三個字母剛好是 "FOR"，若用 `endswith` 判斷會被誤認為
# `DECLARE ... CURSOR FOR` 的形狀，把下一句本來獨立的 SELECT 錯誤地併入前一句、
# 讓那句 SELECT 憑空消失又不留下任何 unsupported 紀錄。改用 `\bFOR\s*$`（前面
# 要求字界，也就是 FOR 前面不能是字母／數字／底線）就能正確排除這類誤判，
# 同時仍然認得出真正的 `... CURSOR FOR` 結尾。
_CURSOR_FOR_SUFFIX = re.compile(r"\bFOR\s*$", re.IGNORECASE)

_SQL_CLAUSE_CONTINUATIONS = frozenset(
    {
        "FROM",
        "WHERE",
        "INTO",
        "VALUES",
        "SET",
        "ORDER",
        "GROUP",
        "HAVING",
        "UNION",
        "AND",
        "OR",
        "ON",
    }
)

# --- 多行 DEFINE ... RECORD ... END RECORD 也不能被 END 切碎 -------------------
#
# genero.lark 的 record_inline 規則支援 `DEFINE name RECORD field, field END
# RECORD` 這種內嵌型別宣告，欄位常常跨好幾行寫。但 `END` 本身在 KNOWN_IGNORED
# 裡（用來認得 END IF／END CASE／END TRY 這類已知但不分析的收尾關鍵字），若
# 天真套用「首字是已知語句關鍵字就開新語句」，`END RECORD` 那一行會把 DEFINE
# 從中間切斷，文法解析會因為看不到收尾的 END RECORD 而失敗。因此：一旦偵測到
# `DEFINE ... RECORD`（且不是 `RECORD LIKE`——那種形式本來就是單行、沒有這個
# 問題）尚未見到對應的 `END RECORD`，中途所有行都併入同一句，直到真的看到
# `END RECORD` 為止。
#
# Task 5 Fix round 1 / Defect 2（有界復原＋強制回報）：上面這條「中途所有行都
# 併入」的規則，若 `END RECORD` 真的永遠沒有出現（漏寫），會把後面整個區塊
# （可能橫跨好幾個其他語句）都吞進同一句，且因為合併後文字餵給 genero.lark
# 會直接解析失敗，落差雖然「有」被回報，但後面所有真正獨立的語句（例如接在
# 後面的 SELECT）也全部消失、指向錯誤的行號與錯誤的原因。因此改為有界復原：
# 一旦在 RECORD 尚未關閉的狀態下遇到一行「首字是已知語句關鍵字」（且這一行
# 本身不是 END RECORD 收尾行），就在那一行之前結束目前這句 DEFINE，讓該關鍵字
# 行去開一句新的、獨立的語句；同時對這句提前結束、始終沒等到 END RECORD 的
# DEFINE，強制回報一筆 UnsupportedSyntax（見 `_UNCLOSED_RECORD_REASON`），
# 而不是嘗試把不完整的文字硬塞給文法解析。
_RECORD_INLINE_OPEN = re.compile(r"\bRECORD\b(?!\s+LIKE\b)", re.IGNORECASE)
_END_RECORD = re.compile(r"\bEND\s+RECORD\b", re.IGNORECASE)
_UNCLOSED_RECORD_REASON = (
    "DEFINE ... RECORD 到語句結束都沒有看到對應的 END RECORD，很可能是漏寫了 END RECORD"
)

# --- Task 5 Fix round 1 / Defect 2（有界復原＋強制回報，括號未配平版本）--------
#
# 原本的規則是「累積文字裡還有未配對的左括號，下一行不管長什麼樣都是延續」，
# 這在括號本來就會配平的正常寫法（`CALL foo(a,` 換行接 `b)`）沒有問題；但如果
# 原始碼本身括號沒配平（漏寫一個 `)`，或本工具誤數了字串／註解外的括號），
# 這條規則會把後面所有行一路併到區塊結尾，吞掉的內容如果剛好首字落在
# KNOWN_IGNORED 裡（例如 CALL），下游完全不會再檢查那句合併文字，於是被吞掉的
# `DEFINE`／`SELECT` 等真正的語句就毫無痕跡地消失、連一筆 unsupported 都不會有。
# 這比 masking.py 對未關閉 `{ ... }` 的處理（撐大範圍但一定回報）更糟：這裡
# 撐大範圍之後完全靜默。因此改為有界復原：括號未配平時，下一行若首字是已知
# 語句關鍵字，一樣結束目前這句（不管併入的是什麼字），讓該關鍵字行去開一句
# 新的語句；同時對這句提前結束、括號始終沒配平的語句，強制回報一筆
# UnsupportedSyntax（見 `_UNBALANCED_PAREN_REASON`），不管它的第一個字是不是
# 在 KNOWN_IGNORED 裡——KNOWN_IGNORED 不該是一個能讓真正落差安靜消失的漏洞。
# 括號真的有配平的情況（`CALL foo(a,` / `b)`）不受影響：延續行的第一個字通常
# 不是語句關鍵字，一樣會被判定為延續，行為與修正前完全相同。
_UNBALANCED_PAREN_REASON = (
    "括號未配平：這句語句從開始到結束，左右括號數量對不上，很可能是漏寫了一個 )"
)

# Task 5 Fix round 2 / Defect 3（小修：負的括號差也要回報）------------------------
#
# 上面的 `_UNBALANCED_PAREN_REASON` 只在 `paren_balance > 0`（左括號比右括號多）
# 時觸發，但左括號比右括號少（多寫了一個沒有對應左括號的 `)`，或原始碼本身就
# 貼錯了位置）從來沒有被檢查過，這句語句就這樣悄悄少了一筆本來該有的回報。
# 這不會造成吞行（`paren_would_continue` 只在 `paren_balance > 0` 時才會讓下一行
# 被併入，負值不會觸發延續），純粹是回報缺口，因此獨立給一個原因字串、
# 不影響既有的正括號差行為。
_NEGATIVE_UNBALANCED_PAREN_REASON = (
    "括號未配平：這句語句出現一個沒有對應左括號的 )，左括號比右括號少，"
    "很可能是多寫了一個 ) 或貼錯了位置"
)

# --- Task 5 Fix round 2 / Defect 3：SQL 續行的猜測改成語法未完成訊號 ------------
#
# 見模組頂端 docstring 例外 2 的說明：舊規則「SQL 累積中，只要下一行第一個字
# 不在 _STATEMENT_KEYWORDS 就視為延續」是個猜測，COMMIT／ROLLBACK／EXECUTE 這些
# 交易或動態 SQL 語句接在一句已經完整的 SQL 語句後面時，會被這個猜測誤判成延續、
# 悄悄併入前一句而消失，且不留下任何 unsupported 紀錄——這正是本模組不能接受的
# 「安靜地產出錯誤結果」。列舉更多關鍵字無法根治這個問題（清單永遠列不完）；
# 真正可靠的訊號是「這句話寫完了嗎」這個語法事實本身，思路與括號配平判斷
# （例外 1）完全一致。因此改用：累積到目前為止的文字，去掉尾端空白後，是否
# 以下列任一種「不可能是一句話結尾」的 token 收尾——
#   - 逗號 `,`、左括號 `(`
#   - 算術／比較運算子：`=`／`<`／`>`／`<=`／`>=`／`!=`／`<>`／`+`／`-`／`*`／`/`／`||`
#   - 一個字尾字：AND／OR／NOT／IN／LIKE／BETWEEN／IS／AS／BY（字界比對，
#     不分大小寫——避免像 `mgr_for` 那樣的識別字尾誤觸發，做法與 `_CURSOR_FOR_SUFFIX`
#     的字界比對同一個道理）
# 只有在上述任一種情況成立時，才視為「這句話還沒寫完」，下一行不管長什麼樣
# 都該視為延續。這個判斷同時也用在「語句結束時仍語法不完整」的回報（見
# `_pending_group_anomaly_reason`）：不管是被下一個關鍵字行結束、還是撐到區塊
# 結尾，只要收尾仍是上述 token 之一，就代表原始碼很可能漏寫了後續內容。
_TRAILING_OPERATOR_OR_PUNCT = re.compile(r"(?P<tok>,|\(|<=|>=|!=|<>|\|\||=|<|>|\+|-|\*|/)\s*$")
_TRAILING_JOINING_WORD = re.compile(
    r"\b(?P<tok>AND|OR|NOT|IN|LIKE|BETWEEN|IS|AS|BY)\s*$", re.IGNORECASE
)

_INCOMPLETE_STATEMENT_REASON_TEMPLATE = (
    "這句語句在結束時仍以「{token}」收尾，語法上看起來還沒寫完（例如漏寫了後面的"
    "欄位、條件或運算元），很可能是原始碼漏寫了後續內容"
)


def _trailing_incomplete_token(text: str) -> str | None:
    """回傳累積文字（去掉尾端空白後）結尾的「語法未完成」token；沒有就回傳 None。

    只檢查逗號／左括號／算術或比較運算子／特定字尾字（見模組上方常數定義的
    說明），這是刻意縮小範圍的清單——目的不是理解完整的 SQL 或 4GL 文法，
    而是抓住「這句話明顯還沒寫完」這個語法事實，用來取代舊版的猜測式判斷。
    """
    trimmed = text.rstrip()
    if not trimmed:
        return None
    match = _TRAILING_OPERATOR_OR_PUNCT.search(trimmed)
    if match:
        return match.group("tok")
    match = _TRAILING_JOINING_WORD.search(trimmed)
    if match:
        return match.group("tok").upper()
    return None


def _statement_looks_incomplete(text: str) -> bool:
    """`text`（累積到目前為止的語句文字）結尾是否有語法上明顯未完成的訊號。"""
    return _trailing_incomplete_token(text) is not None


def _paren_delta(text: str) -> int:
    """`(` 減 `)` 的數目差。用來判斷一句語句是否還有未配對的左括號待續行。"""
    return text.count("(") - text.count(")")


def _pending_group_anomaly_reason(
    *,
    paren_balance: int,
    in_open_record: bool,
    accumulated_text: str,
    first_word: str | None,
) -> str | None:
    """語句累積結束（不管是被下一句關鍵字行結束、還是撐到區塊結尾）時，
    依序檢查括號未配平（正／負兩種方向）、未關閉的 RECORD、結尾語法明顯未完成
    這四種情況，回傳第一個成立的回報原因；全部都不成立就回傳 None。

    順序刻意固定：括號（不管正負）優先於 RECORD，RECORD 優先於「結尾語法未完成」
    ——前面的訊號更具體、更通用，一句語句只回報一筆 anomaly，不重複計算
    （Task 5 Fix round 1 / Defect 2 的括號／RECORD 判斷維持原樣；Task 5 Fix
    round 2 / Defect 3 補上負括號差與結尾語法未完成兩種訊號）。

    Task 5 Fix round 3 / Defect（本輪修正）：「結尾語法明顯未完成」這個訊號
    （逗號／左括號／運算子／AND OR NOT 這些字結尾）只在 `first_word` 落在
    `SQL_KEYWORDS` 裡（這句是 SQL）時才檢查——這個訊號在「延續」用途上
    （`_group_statement_lines` 的條件 3）本來就正確地只在 `is_sql_in_progress`
    時生效，但「回報」用途原本沒有做一樣的限制，導致每一種語句都會被檢查。
    逗號／運算子結尾在 SQL 以外的 4GL 語句裡經常是完全合法的語法，最明顯的例子
    是 `PRINT col1, col2,`：REPORT 裡結尾逗號會抑制換行，讓下一個 PRINT 接在
    同一行輸出，這不代表漏寫了後續內容。若不分語句種類一律檢查，這類合法寫法
    會被誤報成「語法未完成」，把不支援清單灌滿雜訊——這正是本模組開頭 docstring
    講的「誠實回報必須分清楚，否則不支援清單會被雜訊淹沒」。括號未配平／
    RECORD 未關閉這兩種訊號則不受影響：它們不是「這句話讀起來像沒寫完」這種
    看語意的訊號，而是純粹的結構計數（左右括號數量、RECORD 有沒有對應的 END
    RECORD），對任何語句種類都同樣成立，因此繼續套用在所有語句上。
    """
    if paren_balance > 0:
        return _UNBALANCED_PAREN_REASON
    if paren_balance < 0:
        return _NEGATIVE_UNBALANCED_PAREN_REASON
    if in_open_record:
        return _UNCLOSED_RECORD_REASON
    if first_word in SQL_KEYWORDS:
        token = _trailing_incomplete_token(accumulated_text)
        if token is not None:
            return _INCOMPLETE_STATEMENT_REASON_TEMPLATE.format(token=token)
    return None


class _DefineTransformer(Transformer):
    def qualified(self, children):
        return ".".join(str(c) for c in children)

    def record_like(self, children):
        return f"RECORD LIKE {children[0]}"

    def like_ref(self, children):
        return f"LIKE {children[0]}"

    def simple_type(self, children):
        base = str(children[0]).upper()
        nums = [str(c) for c in children[1:]]
        return f"{base}({','.join(nums)})" if nums else base

    def array_type(self, children):
        size, inner = children
        return f"ARRAY[{size}] OF {inner}"

    def field_decl(self, children):
        return f"{children[0]} {children[1]}"

    def record_inline(self, children):
        return f"RECORD({', '.join(children)})"

    def type_ref(self, children):
        return children[0]

    def name_list(self, children):
        return [str(c) for c in children]

    def define_stmt(self, children):
        names, type_text = children
        return [(name, type_text) for name in names]

    def start(self, children):
        return children[0]


def _first_word(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    token = stripped.split(maxsplit=1)[0]
    return "".join(ch for ch in token if ch.isalnum() or ch == "_").upper()


def _group_statement_lines(
    lines: list[tuple[int, str]],
) -> tuple[list[list[int]], dict[int, str]]:
    """依關鍵字切分規則（含上述例外）把行號分組，每組是一句邏輯語句涵蓋的行號。

    回傳 `(groups, anomalies)`：`groups` 是分組結果；`anomalies` 是
    `{該組起始行號: 回報原因}`，記錄哪些組別在結束時（不管是被下一句關鍵字行
    結束、還是撐到區塊結尾）仍留著未配平的括號或未關閉的 `DEFINE ... RECORD`
    ——呼叫端（`parse_block`）必須把這些回報成 `UnsupportedSyntax`，見下方
    Fix round 1 / Defect 2 說明；`logical_statements` 這種只在乎分組本身的
    呼叫端可以忽略這個回傳值。

    輸入的 `lines` 應該已經是遮罩過的文字（呼叫端負責這件事，本函式不重新遮罩）；
    空白（去除前後空白後為空字串）的行會被整行略過，既不計入任何一句，也不會
    自己開一句——這對應「整行都是註解」遮罩後變成空白行的情況。

    一行是否併入前一句（而不是開一句新的），依序檢查四個條件，只要其中一個
    成立就併入；全部不成立才會開新的一句：
      1. 目前正在累積的語句還在一個尚未關閉的 `DEFINE ... RECORD` 裡
         （`in_open_record`，見模組頂端 `_RECORD_INLINE_OPEN`／`_END_RECORD`），
         **且**這一行不是真正的 `END RECORD` 收尾行時，這一行的第一個字
         也不能是已知語句關鍵字（`_STATEMENT_KEYWORDS`）——欄位名稱正常情況下
         不會撞到語句關鍵字，但若真的撞到（或 `END RECORD` 始終沒出現），
         代表這句 RECORD 已經無法再合理地延續下去，必須在這裡結束並回報
         （見 Fix round 1 說明）。真正的 `END RECORD` 收尾行永遠視為延續，
         不受這條限制影響。
      2. 目前累積的文字裡還有未配對的左括號（`paren_balance > 0`）——這是最
         通用的「這句話還沒寫完，下一行不管長什麼樣都是延續」訊號，涵蓋
         `CALL foo(a,` 換行接 `b)` 這類常見的多行參數列，不必為每一種語句
         各自寫規則。但**這一行的第一個字不能是已知語句關鍵字**：若原始碼
         本身括號沒配平（真正的語法錯誤，或本工具誤數了字串／註解外的括號），
         遇到下一句語句關鍵字時就該結束目前這句並回報，而不是把後面所有行
         一路併到區塊結尾又完全靜默（見 Fix round 1 / Defect 2 說明）。
      3. 目前正在累積一句 SQL（`accumulated_first_word` 是 `SQL_KEYWORDS`
         之一），而這一行的第一個字要嘛是已知的 SQL 子句關鍵字
         （`_SQL_CLAUSE_CONTINUATIONS`），要嘛累積到目前為止的文字本身
         「語法上明顯還沒寫完」（`_statement_looks_incomplete`：結尾是逗號、
         左括號、算術／比較運算子，或 AND／OR／NOT／IN／LIKE／BETWEEN／IS／AS／BY
         這些字尾字）。Task 5 Fix round 2 / Defect 3：後半段原本是「這一行的
         第一個字根本不是任何已知語句關鍵字」——這是個猜測，`COMMIT`／
         `ROLLBACK`／`EXECUTE` 這些交易或動態 SQL 語句接在一句已完整的 SQL
         語句後面時會被誤判成延續、悄悄併入前一句而消失，且不留下任何
         unsupported 紀錄。改用語法未完成這個真正的訊號後，`COMMIT` 等語句
         本身現在也已經是 `_STATEMENT_KEYWORDS` 的一員（見 `KNOWN_IGNORED`），
         而「上一句 SQL 是否已經寫完」改用事實判斷，不再用猜的。
      4. `DECLARE ... CURSOR FOR` 換行後緊接著 `SELECT`（見上方常數定義的
         說明；`FOR` 結尾判斷用字界比對，見 `_CURSOR_FOR_SUFFIX`）。

    除了這四種情況，一行的第一個字若是已知語句關鍵字、或這是檔案的第一句，
    就開新的一句；**否則**（無法辨識的關鍵字，且不落在上述任何一種結構性
    延續情境裡）**也一律開新的一句**，而不是天真地併入前一句——理由見模組
    頂端 docstring：併入前一句在這種情況下會把一個誠實的「我不認得這句」
    誤報成別的、講錯原因的錯誤。

    Fix round 1 / Defect 2（有界復原＋強制回報，統一說明）：條件 1、2 原本
    都是「不管下一行長什麼樣都無條件延續」，這對真正配平／真正等到 END RECORD
    的正常寫法沒問題，但對「永遠不會配平／永遠不會等到 END RECORD」的原始碼
    （通常是漏寫了一個 `)` 或漏寫了 `END RECORD`）會把後面整個區塊都吞掉且
    完全不留痕跡——尤其當吞掉的語句第一個字剛好在 KNOWN_IGNORED 裡
    （例如 `CALL`），下游根本不會再去檢查那句合併文字，落差就會零聲張地消失，
    這正是本模組不能接受的「安靜地產出錯誤結果」。因此兩個條件都改為有界：
    一旦下一行的第一個字是已知語句關鍵字，就結束目前這句、讓該關鍵字行去開
    一句新的、獨立的語句；同時把「結束時仍未配平／未關閉」的狀態記錄進
    `anomalies`，不管目前這句的第一個字是不是在 KNOWN_IGNORED 裡——這正是
    避免 KNOWN_IGNORED 變成一個能讓真正落差安靜消失的漏洞。

    Task 5 Fix round 2 / Defect 3：條件 3 的後半段原本是「這一行的第一個字
    不是已知語句關鍵字就當延續」，這同樣是個猜測，且與 Fix round 1 修的兩個
    無界延續問題是同一種失敗模式——只是這次猜錯的後果不是吞一整塊，而是吞掉
    緊接在一句完整 SQL 之後的下一句語句（`COMMIT`／`ROLLBACK`／`EXECUTE` 等）。
    改用「累積文字結尾是否語法明顯未完成」這個事實訊號取代猜測，思路與括號
    配平判斷一致；同時把常見的交易／動態 SQL 語句加進 `KNOWN_IGNORED`，讓它們
    在「猜測」被拿掉之後仍然能被正確辨識為已知語句，而不是變成無法辨識。
    另外新增：一句語句結束時（不管是被下一個關鍵字行結束、還是撐到區塊結尾），
    若結尾仍是上述「語法未完成」token 之一，強制回報一筆 UnsupportedSyntax
    （見 `_pending_group_anomaly_reason` 裡的 `_trailing_incomplete_token`
    檢查），並補上負的括號差（多寫了一個 `)`）也要回報——兩者都是純粹的回報
    缺口修正，不影響任何既有的延續／分組行為。
    """
    groups: list[list[int]] = []
    anomalies: dict[int, str] = {}
    accumulated_first_word: str | None = None
    accumulated_text = ""
    in_open_record = False
    paren_balance = 0
    current_start_line: int | None = None

    for line_no, text in lines:
        stripped = text.strip()
        if not stripped:
            continue
        word = _first_word(stripped)
        is_sql_in_progress = accumulated_first_word in SQL_KEYWORDS
        is_sql_clause_continuation = is_sql_in_progress and (
            word in _SQL_CLAUSE_CONTINUATIONS or _statement_looks_incomplete(accumulated_text)
        )
        is_cursor_for_select = (
            is_sql_in_progress
            and word == "SELECT"
            and bool(_CURSOR_FOR_SUFFIX.search(accumulated_text))
        )
        is_end_record_line = in_open_record and bool(_END_RECORD.search(stripped))
        record_would_continue = in_open_record and (
            is_end_record_line or word not in _STATEMENT_KEYWORDS
        )
        paren_would_continue = paren_balance > 0 and word not in _STATEMENT_KEYWORDS
        # --- C2（branch review）：續行判斷通用化，不再限定「目前累積的是 SQL」---
        #
        # 上面的 `is_sql_clause_continuation` 只在 `is_sql_in_progress` 時才會用
        # 「結尾語法明顯未完成」這個訊號延續。這對非 SQL 的普通 4GL 多行語句
        # （`PRINT col1,` / `col2`、`LET x = a +` / `b`、`IF a IS NULL` /
        # `OR b IS NULL THEN`、`WHILE a > 0` / `AND b < 9`）完全沒有涵蓋：續行
        # 的第一個字通常不落在 `_STATEMENT_KEYWORDS` 裡（欄位名、識別字、運算元
        # 都不是關鍵字），於是被誤判成一句新的、無法辨識的語句，把
        # `parse_status` 拖成 `partial`——這正是本模組不能接受的「安靜地產出
        # 錯誤結果」的相反面：明明語法完全合法的續行，卻被誤報成假的落差。
        #
        # 修正加兩條延續規則，套用在任何正在累積的語句上（不分 SQL 或非
        # SQL）：
        #   (a) 續行第一個字不是已知語句關鍵字，且累積文字結尾語法明顯未完成
        #       （`_statement_looks_incomplete`：逗號／左括號／算術或比較運算子
        #       ／AND OR NOT IN LIKE BETWEEN IS AS BY 這些字尾）——`word not in
        #       _STATEMENT_KEYWORDS` 這個條件必須保留：沒有它，`PRINT col1,
        #       col2,` 後面接 `PRINT col3, col4` 會因為累積文字結尾是逗號，把
        #       第二個 PRINT 也吞進來，讓兩句合法獨立的語句被誤併成一句。
        #   (b) 續行第一個字本身就是 AND／OR／NOT——`IF a IS NULL` 這句結尾是
        #       `NULL`，並沒有觸發規則 (a) 的「結尾未完成」訊號，只能靠續行
        #       本身的字面判斷；但沒有任何 4GL 語句會以 AND／OR／NOT 開頭，
        #       這條規則因此永遠安全，不會把真正該獨立成一句的語句誤併。
        #
        # 「回報」（`_pending_group_anomaly_reason`）用途維持原樣，不受這裡的
        # 影響：它仍然只在語句的第一個字落在 `SQL_KEYWORDS` 時才檢查「結尾
        # 語法未完成」，PRINT／DISPLAY 這類合法的逗號續行慣用語（Task 5 Fix
        # round 3 已經處理過）不會因為這裡的續行判斷通用化而被改變行為。
        looks_incomplete_continuation = word not in _STATEMENT_KEYWORDS and (
            _statement_looks_incomplete(accumulated_text)
        )
        is_and_or_not_continuation = word in {"AND", "OR", "NOT"}
        continues_previous_statement = groups and (
            record_would_continue
            or paren_would_continue
            or is_sql_clause_continuation
            or is_cursor_for_select
            or looks_incomplete_continuation
            or is_and_or_not_continuation
        )
        starts_new = not continues_previous_statement

        if starts_new:
            if current_start_line is not None:
                reason = _pending_group_anomaly_reason(
                    paren_balance=paren_balance,
                    in_open_record=in_open_record,
                    accumulated_text=accumulated_text,
                    first_word=accumulated_first_word,
                )
                if reason is not None:
                    anomalies[current_start_line] = reason
            groups.append([line_no])
            current_start_line = line_no
            accumulated_first_word = word if word in _STATEMENT_KEYWORDS else None
            accumulated_text = stripped
            paren_balance = _paren_delta(stripped)
            in_open_record = (
                word == "DEFINE"
                and bool(_RECORD_INLINE_OPEN.search(stripped))
                and not _END_RECORD.search(stripped)
            )
        else:
            groups[-1].append(line_no)
            accumulated_text = f"{accumulated_text} {stripped}"
            paren_balance += _paren_delta(stripped)
            if in_open_record and _END_RECORD.search(stripped):
                in_open_record = False

    if current_start_line is not None:
        reason = _pending_group_anomaly_reason(
            paren_balance=paren_balance,
            in_open_record=in_open_record,
            accumulated_text=accumulated_text,
            first_word=accumulated_first_word,
        )
        if reason is not None:
            anomalies[current_start_line] = reason

    return groups, anomalies


def logical_statements(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """把 (行號, 行文字) 收攏成 (起始行號, 單行化的語句文字)。

    呼叫端應該傳入已經遮罩過的行（例如 `Block.masked_lines`），這樣語句邊界
    判斷才不會被註解或字串常值裡剛好長得像關鍵字的文字誤導；本函式本身
    不做遮罩，也不做註解剝除（遮罩後的整行註解已經是空白行，直接被跳過）。

    括號未配平／RECORD 未關閉的回報（`_group_statement_lines` 回傳的
    `anomalies`）不是這個函式的職責——那是 `parse_block` 對 `ProgramUnit`
    的責任，這裡只在乎分組與拼接文字本身，因此忽略該回傳值。
    """
    groups, _anomalies = _group_statement_lines(lines)
    text_by_line = dict(lines)
    return [(group[0], " ".join(text_by_line[ln].strip() for ln in group)) for group in groups]


def parse_definitions(statement: str, line: int) -> tuple[list[Definition], str | None]:
    """解析一句 DEFINE。回傳 (Definition 清單, 失敗原因)；成功時原因為 None。"""
    try:
        pairs = _DefineTransformer().transform(_GRAMMAR.parse(statement))
    except LarkError as exc:
        return [], f"DEFINE 解析失敗：{type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001 - parser 絕不可讓整個 scan 中止
        return [], f"DEFINE 解析發生未預期錯誤：{type(exc).__name__}"
    return [Definition(name=name, type=type_text, line=line) for name, type_text in pairs], None


def parse_block(block: Block) -> ProgramUnit:
    """解析一個程式單元。本函式在任何輸入下都不會拋出例外。"""
    definitions: list[Definition] = []
    sql: list[SqlStatement] = []
    unsupported: list[UnsupportedSyntax] = []
    understood = 0

    raw_by_line: dict[int, str] = dict(block.lines)
    masked_by_line: dict[int, str] = dict(block.masked_lines)
    groups, group_anomalies = _group_statement_lines(block.masked_lines)

    for group in groups:
        line_no = group[0]
        statement = " ".join(masked_by_line[ln].strip() for ln in group)
        # snippet 一律用原始文字，讓不支援清單裡的片段是使用者看得懂的真實原始碼，
        # 而不是換成空白的遮罩版本；raw_by_line 理論上與 masked_by_line 行號集合
        # 完全相同（兩者都是 Block 同一份 body 算出來的），這裡的 .get 只是防禦性寫法。
        raw_statement = " ".join(raw_by_line.get(ln, masked_by_line[ln]).strip() for ln in group)
        word = _first_word(statement)

        # Task 5 Fix round 1 / Defect 2：這句語句是被「括號未配平」或「RECORD
        # 未關閉」提前結束的（見 `_group_statement_lines` 回傳的 `group_anomalies`）。
        # 這時累積到的文字本身就不完整，不再嘗試把它交給 DEFINE 文法解析或
        # SQL 擷取——那只會得到一個指向錯誤原因的第二筆回報；改成只回報這一筆
        # 結構性的 anomaly，且**不管這句的第一個字是不是在 KNOWN_IGNORED
        # 裡**都要回報，因為這正是「KNOWN_IGNORED 開頭的語句能讓真正落差
        # 安靜消失」這個漏洞本身。
        anomaly_reason = group_anomalies.get(line_no)
        if anomaly_reason is not None:
            unsupported.append(
                UnsupportedSyntax(
                    file=block.file,
                    line=line_no,
                    snippet=snippet(raw_statement),
                    reason=anomaly_reason,
                )
            )
            continue

        if word == "DEFINE":
            parsed, reason = parse_definitions(statement, line_no)
            if reason is None:
                definitions.extend(parsed)
                understood += 1
            else:
                unsupported.append(
                    UnsupportedSyntax(
                        file=block.file,
                        line=line_no,
                        snippet=snippet(raw_statement),
                        reason=reason,
                    )
                )
            continue

        if word in SQL_KEYWORDS:
            extracted = extract_sql(statement, line_no)
            if extracted is not None:
                sql.append(extracted)
                understood += 1
            else:
                unsupported.append(
                    UnsupportedSyntax(
                        file=block.file,
                        line=line_no,
                        snippet=snippet(raw_statement),
                        reason=f"{word} 語句形式不在 MVP 的擷取範圍",
                    )
                )
            continue

        if word in KNOWN_IGNORED:
            understood += 1
            continue

        unsupported.append(
            UnsupportedSyntax(
                file=block.file,
                line=line_no,
                snippet=snippet(raw_statement),
                reason="無法辨識的語句關鍵字",
            )
        )

    if not block.terminated:
        parse_status = "failed"
    elif not unsupported:
        parse_status = "ok"
    elif understood == 0:
        parse_status = "failed"
    else:
        parse_status = "partial"

    return ProgramUnit(
        kind=block.kind,
        name=block.name,
        file=block.file,
        start_line=block.start_line,
        end_line=block.end_line,
        parse_status=parse_status,
        definitions=definitions,
        sql=sql,
        unsupported=unsupported,
    )
