"""把 .4gl 原始碼切成頂層程式單元（MAIN／FUNCTION／REPORT／GLOBALS）。

這一層刻意**不使用文法**，也刻意**不會失敗**。理由：scan 的第一要求是
「永不因單檔失敗而中止」。先用可靠的關鍵字切分取得骨架，再由 parser.py
對每個區塊內部逐句嘗試解析——解析不了的句子只會影響那一句，
不會讓其他區塊、其他檔案跟著陪葬。

已知限制：以行為單位辨識區塊起訖，因此同一行寫多個區塊關鍵字的程式無法正確切分。
真實 T100 程式未見此寫法；若遇到，請開 parser-gap-report issue。

遮罩（masking）：所有關鍵字／結構比對（`DATABASE`、區塊起始、`END <KIND>`、
內層嚴格 header、整行是否為註解）一律不是對原始行文字比對，而是對
`masking.mask_comments_and_strings` 產生的一份「遮罩後」副本比對——把屬於
註解或字串常值的字元換成空白，但保留完全相同的行數與換行字元，每一行的
長度也不變。這樣「程式碼裡剛好長得像關鍵字的註解或字串內容」就不會誤觸
任何結構判斷。`Block.lines`、`snippet()` 用的仍然是原始（未遮罩）文字，
使用者看到的永遠是真正的原始碼。遮罩範圍、未終止復原規則、`{ ... }` 忘記
寫對應 `}` 時的偵測與殘留限制，細節見 `masking.py` 模組 docstring。

`Block.masked_lines` 把這份遮罩後副本也交給呼叫端：`parser.py`（Task 5）逐句
切分與解析時，同樣必須用遮罩後的文字判斷「這一行是不是某個已知語句關鍵字
開頭」，理由與這裡完全一樣——原始文字裡的註解或字串常值不該誤觸判斷。
`masked_lines` 與 `lines` 一一對應：同樣的行號、同樣的行數，且排除區塊自己的
開頭／結尾關鍵字行的規則也完全相同。

未終止區塊的復原策略：掃描對應 END <KIND> 的過程中，若先遇到另一個區塊的起始
關鍵字，代表目前區塊沒有正常結束——因為 Genero 4GL 這四種頂層構造彼此不會
巢狀。此時目前區塊會在新 opener 前一行結束（`terminated=False`），並記錄一筆
`UnsupportedSyntax`；外層掃描會從該 opener 行重新開始，讓它成為獨立的程式單元。
如此可把「壞掉」的範圍限制在原本那一個區塊，不會把後面的區塊一併吞掉，也不會
謊報原區塊已正常結束。若掃到檔尾都沒有對應 END 也沒有其他 opener，則沿用原本
行為：`terminated=False`、`end_line` 設為檔尾行號，並記錄 `UnsupportedSyntax`。

已知限制（區塊內部 opener 判斷改用嚴格標頭樣式）：上述「內層」opener 偵測必須
是完整合法的區塊標頭才會觸發——`MAIN` 單獨一行；`GLOBALS` 單獨一行或後接字串
常數（include 形式）；`FUNCTION`／`REPORT` 後接單一識別字、可選括號參數列，
行尾只允許接行內註解。body 裡剛好以這四個字開頭的普通敘述句（例如 RECORD
欄位命名為 `main`，或文件式文字「Report the totals...」）不再被誤判為 opener，
因此不會把合法區塊切碎。頂層（外層迴圈）的判斷則刻意維持寬鬆，未套用此樣式：
在頂層一行開頭是這四個關鍵字沒有其他合理解讀，即使關鍵字後面沒有名稱或參數，
也應誠實回報為一個（寫壞的）區塊，而非漏掉不報。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from erp_migrai.inventory.masking import mask_comments_and_strings
from erp_migrai.inventory.models import UnsupportedSyntax, snippet

_BLOCK_START = re.compile(
    r"^\s*(?P<kind>GLOBALS|MAIN|FUNCTION|REPORT)\b\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
# 嚴格版的區塊起始樣式，只用於「內層」掃描對應 END 時的 opener 判斷。
#
# 頂層判斷（外層迴圈）刻意維持寬鬆的 _BLOCK_START：一行開頭是這四個關鍵字之一，
# 在頂層沒有其他合理解讀——就算 FUNCTION 後面沒寫名稱，也理應被當成一個
# （寫壞的）FUNCTION 區塊來誠實回報 name=None，而不是漏掉它。
#
# 但在區塊「內部」，同一個寬鬆判斷會誤傷：body 裡完全合法的敘述句只是剛好開頭
# 撞到這四個字（例如 RECORD 欄位命名為 main、註解式文字「Report the totals...」），
# 這種情況遠比真的巢狀 opener 常見，若沿用寬鬆判斷會把好端端的區塊切碎、
# 產生根本不存在的假 anomaly。因此內層 opener 偵測改用這個嚴格樣式：
# 一行必須「就是」一個合法的區塊標頭（MAIN 單獨一行／GLOBALS 單獨一行或後接
# 字串常數的 include 形式／FUNCTION 或 REPORT 後接單一識別字、可選括號參數列——
# 參數列允許在這一行只開了 `(` 沒有寫完對應的 `)`，因為 4GL 常見把落落長的參數
# 清單拆成好幾行寫），其餘一律視為普通敘述句、不當作 opener。
#
# 這個樣式一律拿「遮罩後」的行文字去比對（見 `masking.mask_comments_and_strings`），
# 因此不需要再自己處理行尾註解：遮罩會把 `#`／`--`／`{ ... }` 都換成空白，變成
# 單純的行尾空白，交給樣式結尾的 `\s*$` 處理即可。
_STRICT_BLOCK_HEADER = re.compile(
    r"""^\s*
    (?:
        (?P<kind1>MAIN)
        |
        (?P<kind2>GLOBALS)(?:\s+(?P<include>"[^"]*"|'[^']*'))?
        |
        (?P<kind3>FUNCTION|REPORT)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)
            (?:
                \s*\((?P<params>[^()]*)\)
                |
                \s*\((?P<params_open>[^()]*)
            )?
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BLOCK_END = re.compile(
    r"^\s*END\s+(?P<kind>GLOBALS|MAIN|FUNCTION|REPORT)\b",
    re.IGNORECASE,
)
_DATABASE = re.compile(r"^\s*DATABASE\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_NAME = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class Block:
    kind: str
    name: str | None
    file: str
    start_line: int
    end_line: int
    lines: list[tuple[int, str]] = field(default_factory=list)
    terminated: bool = True
    # 與 `lines` 一一對應（同樣的行號、同樣的行數、同一行的字元數也相同）的
    # 遮罩後副本：註解與字串常值內容已被換成空白。`parser.py` 的所有關鍵字／
    # 語句切分判斷都必須用這個欄位，不能用 `lines`——道理與 `blocks.py` 自己
    # 判斷區塊起訖時一樣：原始文字裡剛好長得像關鍵字的註解或字串內容不該誤觸
    # 判斷。`lines` 仍然保留原始文字，只給 `snippet()` 這種要給使用者看的用途用。
    masked_lines: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class SplitResult:
    blocks: list[Block] = field(default_factory=list)
    database: str | None = None
    unsupported: list[UnsupportedSyntax] = field(default_factory=list)


def _block_name(kind: str, rest: str) -> str | None:
    """FUNCTION／REPORT 的名稱取關鍵字後的第一個識別字；MAIN／GLOBALS 無名稱。"""
    if kind in ("MAIN", "GLOBALS"):
        return None
    match = _NAME.match(rest.strip())
    return match.group("name") if match else None


def split_blocks(text: str, *, source_file: str) -> SplitResult:
    """切分頂層程式單元。本函式在任何輸入下都不會拋出例外。"""
    result = SplitResult()
    lines = text.splitlines()
    masked_text, mask_unsupported = mask_comments_and_strings(text, source_file=source_file)
    masked_lines = masked_text.splitlines()
    # 遮罩只會把字元換成空白，絕不新增／刪除換行，理論上行數必然相同；這裡仍加一道
    # 防禦性補齊，確保就算真的踩到未預期的邊界情況，索引也不會超出範圍而拋例外。
    if len(masked_lines) < len(lines):
        masked_lines = masked_lines + [""] * (len(lines) - len(masked_lines))
    result.unsupported.extend(mask_unsupported)
    index = 0
    total = len(lines)

    while index < total:
        raw = lines[index]
        masked = masked_lines[index]
        line_no = index + 1

        if not masked.strip():
            index += 1
            continue

        database_match = _DATABASE.match(masked)
        if database_match:
            if result.database is None:
                result.database = database_match.group("name")
            index += 1
            continue

        start_match = _BLOCK_START.match(masked)
        if not start_match:
            result.unsupported.append(
                UnsupportedSyntax(
                    file=source_file,
                    line=line_no,
                    snippet=snippet(raw),
                    reason="頂層出現無法辨識的敘述，不屬於任何程式單元",
                )
            )
            index += 1
            continue

        kind = start_match.group("kind").upper()
        rest = start_match.group("rest")

        # GLOBALS "file.4gl" 是 include 形式，單行即結束，沒有 END GLOBALS。
        if kind == "GLOBALS" and rest.strip().startswith(('"', "'")):
            result.blocks.append(
                Block(
                    kind=kind,
                    name=None,
                    file=source_file,
                    start_line=line_no,
                    end_line=line_no,
                )
            )
            index += 1
            continue

        body: list[tuple[int, str]] = []
        body_masked: list[tuple[int, str]] = []
        cursor = index + 1
        terminated = False
        opener_hit = False
        end_line = line_no

        while cursor < total:
            current_raw = lines[cursor]
            current_masked = masked_lines[cursor]
            end_match = _BLOCK_END.match(current_masked)
            if end_match and end_match.group("kind").upper() == kind:
                terminated = True
                end_line = cursor + 1
                cursor += 1
                break
            # Genero 4GL 的 GLOBALS／MAIN／FUNCTION／REPORT 都是頂層構造，彼此不會
            # 巢狀出現。因此掃描對應 END 的過程中，若先遇到另一個區塊的起始關鍵字，
            # 必定代表目前這個區塊沒有正常結束（不可能是合法的巢狀語法）。此時讓
            # 目前區塊在這裡結束並回報，讓外層迴圈從這個新的 opener 行重新開始，
            # 使它成為獨立的程式單元——避免整段被悄悄吞掉、又謊報原區塊已終止。
            if _STRICT_BLOCK_HEADER.match(current_masked):
                opener_hit = True
                end_line = cursor  # 前一行才是目前區塊實際結尾的行號
                break
            body.append((cursor + 1, current_raw))
            body_masked.append((cursor + 1, current_masked))
            cursor += 1

        if not terminated:
            if opener_hit:
                reason = (
                    f"{kind} 區塊尚未寫 END {kind} 就遇到新的區塊起始關鍵字，"
                    f"視為 {kind} 區塊未正常結束"
                )
            else:
                end_line = total
                reason = f"{kind} 區塊未正常結束（找不到對應的 END {kind}）"
            result.unsupported.append(
                UnsupportedSyntax(
                    file=source_file,
                    line=line_no,
                    snippet=snippet(raw),
                    reason=reason,
                )
            )

        result.blocks.append(
            Block(
                kind=kind,
                name=_block_name(kind, rest),
                file=source_file,
                start_line=line_no,
                end_line=end_line,
                lines=body,
                terminated=terminated,
                masked_lines=body_masked,
            )
        )
        index = cursor

    return result
